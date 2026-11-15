import argparse
import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from openvpn_addon import audit, config as config_module, main
from openvpn_addon.config import AddonConfig, load_config
from openvpn_addon.pki import PkiManager


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        patcher = patch.object(config_module, "DATA_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = AddonConfig(experimental_audit_log=True)

    def test_disabled_by_default_and_does_not_touch_storage(self):
        config = AddonConfig()
        self.assertFalse(audit.record_event(config, "client_connect", "alice"))
        self.assertEqual({"enabled": False, "events": [], "next_before": None}, audit.read_events(config))
        self.assertFalse(config.audit_path.parent.exists())

    def test_option_loading_and_manifest_default(self):
        options = self.root / "options.json"
        options.write_text('{"experimental_audit_log": "false"}')
        self.assertFalse(load_config(options).experimental_audit_log)
        options.write_text('{"experimental_audit_log": true}')
        self.assertTrue(load_config(options).experimental_audit_log)
        manifest = (Path(__file__).resolve().parents[1] / "addon/config.yaml").read_text()
        self.assertIn("  experimental_audit_log: false", manifest)
        self.assertIn("  experimental_audit_log: bool?", manifest)

    def test_persistence_order_fields_permissions_and_disabled_reads(self):
        for event in sorted(audit.EVENTS):
            self.assertTrue(audit.record_event(self.config, event, "alice"))
        history = audit.read_events(AddonConfig(experimental_audit_log=True))
        self.assertEqual(sorted(audit.EVENTS, reverse=True), [e["event"] for e in history["events"]])
        for event in history["events"]:
            self.assertEqual({"id", "timestamp", "event", "client"}, set(event))
            self.assertEqual("alice", event["client"])
            self.assertTrue(event["timestamp"].endswith("+00:00"))
        self.assertEqual(0o600, self.config.audit_path.stat().st_mode & 0o777)
        self.assertEqual(0o700, self.config.audit_path.parent.stat().st_mode & 0o777)
        self.assertEqual([], audit.read_events(AddonConfig())["events"])
        self.assertTrue(self.config.audit_path.exists())

    def test_retention_and_cursor_ignore_new_arrivals(self):
        with patch.object(audit, "MAX_EVENTS", 5), patch.object(audit, "PAGE_SIZE", 2):
            for number in range(8):
                self.assertTrue(audit.record_event(self.config, "client_connect", f"client{number}"))
            first = audit.read_events(self.config)
            self.assertEqual(["client7", "client6"], [e["client"] for e in first["events"]])
            audit.record_event(self.config, "client_disconnect", "new")
            second = audit.read_events(self.config, first["next_before"])
            self.assertEqual(["client5", "client4"], [e["client"] for e in second["events"]])
            self.assertIsNone(second["next_before"])
            with contextlib.closing(sqlite3.connect(self.config.audit_path)) as db:
                self.assertEqual(5, db.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def test_concurrent_writers_do_not_lose_events(self):
        def record(number):
            return audit.record_event(self.config, "profile_download", f"client{number}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertTrue(all(pool.map(record, range(40))))
        events = audit.read_events(self.config)["events"]
        self.assertEqual(40, len(events))
        self.assertEqual(40, len({event["id"] for event in events}))

    def test_failure_warning_is_redacted_and_corruption_is_not_erased(self):
        self.config.audit_path.parent.mkdir()
        self.config.audit_path.write_bytes(b"not a database")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertFalse(audit.record_event(self.config, "client_connect", "alice"))
            self.assertFalse(audit.record_event(self.config, "unrecognized", "private-input"))
            self.assertFalse(audit.record_event(self.config, "client_connect", "alice\n"))
        self.assertNotIn("alice", output.getvalue())
        self.assertNotIn("private-input", output.getvalue())
        self.assertEqual(b"not a database", self.config.audit_path.read_bytes())
        with self.assertRaises(sqlite3.DatabaseError):
            audit.read_events(self.config)

    def test_storage_error_does_not_raise(self):
        with patch.object(audit.os, "open", side_effect=OSError("private error")):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertFalse(audit.record_event(self.config, "profile_download", "alice"))
        self.assertNotIn("private error", output.getvalue())

    def test_database_lock_does_not_fail_operation(self):
        audit.record_event(self.config, "client_connect", "alice")
        with contextlib.closing(sqlite3.connect(self.config.audit_path)) as db:
            db.execute("BEGIN EXCLUSIVE")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(audit.record_event(self.config, "client_disconnect", "alice"))
            db.rollback()
        self.assertEqual(1, len(audit.read_events(self.config)["events"]))

    def test_successful_revoke_is_recorded_before_crl_refresh(self):
        pki = PkiManager(self.config)
        with patch.object(pki, "client_status", return_value="valid"), patch.object(pki, "_run"):
            with patch.object(pki, "refresh_crl", side_effect=RuntimeError("CRL failed")):
                with self.assertRaises(RuntimeError):
                    pki.revoke_client("alice")
        self.assertEqual("client_revoke", audit.read_events(self.config)["events"][0]["event"])

    def test_failed_or_repeated_revokes_are_not_recorded(self):
        pki = PkiManager(self.config)
        for status in ("missing", "revoked", "valid"):
            with patch.object(pki, "client_status", return_value=status):
                with patch.object(pki, "_run", side_effect=RuntimeError("failed")):
                    with self.assertRaises((FileNotFoundError, ValueError, RuntimeError)):
                        pki.revoke_client("alice")
        self.assertEqual([], audit.read_events(self.config)["events"])

    def test_hook_load_failure_returns_success_without_disclosing_error(self):
        with patch.dict(os.environ, {"script_type": "client-connect", "common_name": "alice"}):
            with patch.object(main, "load_config", side_effect=ValueError("private error")):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(0, main.cmd_audit_event(argparse.Namespace()))
        self.assertNotIn("private error", output.getvalue())

    def test_real_hook_processes_share_history_and_ignore_environment_secrets(self):
        options = self.root / "options.json"
        options.write_text(json.dumps({"experimental_audit_log": True}))
        temporary_config = self.root / "client-config"
        temporary_config.write_text("")
        # OpenVPN builds a restricted environment; the rendered setenv supplies
        # PYTHONPATH. Only the two data-path overrides are specific to this test.
        env = {
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "addon/src"),
            "OPENVPN_ADDON_DATA": str(self.root), "OPENVPN_ADDON_OPTIONS": str(options),
        }
        env.update(common_name="alice", password="synthetic-secret-never-record", trusted_ip="203.0.113.2")
        for kind, extra in [("client-connect", [str(temporary_config)]), ("client-disconnect", [])]:
            env["script_type"] = kind
            process = subprocess.run(
                [sys.executable, "-m", "openvpn_addon.main", "audit-event", *extra],
                env=env, cwd=self.root, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(0, process.returncode)
            self.assertEqual("", process.stdout + process.stderr)
        self.assertEqual("", temporary_config.read_text())
        history = audit.read_events(self.config)
        self.assertEqual(["client_disconnect", "client_connect"], [e["event"] for e in history["events"]])
        self.assertNotIn("synthetic-secret", json.dumps(history))
        self.assertNotIn("203.0.113.2", json.dumps(history))
        self.assertNotIn(b"synthetic-secret", self.config.audit_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
