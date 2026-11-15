import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import openvpn_addon.config as config_module
from openvpn_addon.config import AddonConfig, load_config
from openvpn_addon.crl_renewal import CrlRenewalSchedule, renew_crl_if_due
from openvpn_addon.main import RequestWatcher
from openvpn_addon.pki import PkiManager
from openvpn_addon.validator import config_warnings


class RenewalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        root_patch = patch.object(config_module, "DATA_ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.config = AddonConfig(experimental_crl_auto_renew=True)
        self.config.pki_dir.mkdir()
        (self.config.pki_dir / "index.txt").write_text("revocation database")
        self.config.crl_path.write_text("old CRL")

    def check_at(self, remaining, days=3650):
        self.config.crl_valid_days = days
        # Jan 1, 2030 UTC; fixed time avoids rounding and timezone dependence.
        expiry = 1893456000
        with patch("openvpn_addon.crl_renewal.time.time", return_value=expiry - remaining), \
             patch("openvpn_addon.crl_renewal.subprocess.run", return_value=Mock(
                 stdout="nextUpdate=Jan  1 00:00:00 2030 GMT\n")), \
             patch.object(PkiManager, "_run_unlocked") as signing:
            renewed = renew_crl_if_due(self.config)
            self.assertEqual(renewed, signing.called)
            if renewed:
                signing.assert_called_once_with("gen-crl", cwd=self.root, quiet=True)
            return renewed

    def test_expiry_boundaries_and_expired_crl(self):
        for remaining, expected in [(604801, False), (604800, True), (1, True), (0, True), (-1, True)]:
            with self.subTest(remaining=remaining):
                self.assertEqual(expected, self.check_at(remaining))

    def test_short_lifetime_renews_at_half_life(self):
        self.assertFalse(self.check_at(86400, days=1))
        self.assertFalse(self.check_at(43201, days=1))
        self.assertTrue(self.check_at(43200, days=1))

    def test_disabled_and_non_boolean_flags_refuse_before_io(self):
        for value in (False, None, 1, "true"):
            self.config.experimental_crl_auto_renew = value
            with patch.object(PkiManager, "mutation_lock") as lock:
                with self.assertRaises(ValueError):
                    renew_crl_if_due(self.config)
                with self.assertRaises(ValueError):
                    CrlRenewalSchedule(self.config)
                lock.assert_not_called()

    def test_configuration_default_and_explicit_opt_in(self):
        path = self.root / "options.json"
        for payload, expected in [({}, False), ({"experimental_crl_auto_renew": False}, False),
                                  ({"experimental_crl_auto_renew": True}, True)]:
            path.write_text(json.dumps(payload))
            self.assertIs(load_config(path).experimental_crl_auto_renew, expected)
        for value in ("false", "true", "typo", 1, None, []):
            path.write_text(json.dumps({"experimental_crl_auto_renew": value}))
            with self.assertRaises(ValueError):
                load_config(path)

    def test_missing_crl_is_rebuilt_from_existing_index(self):
        self.config.crl_path.unlink()
        with patch.object(PkiManager, "_run_unlocked") as signing:
            self.assertTrue(renew_crl_if_due(self.config))
            signing.assert_called_once_with("gen-crl", cwd=self.root, quiet=True)

    def test_missing_index_refuses_to_reset_revocations(self):
        (self.config.pki_dir / "index.txt").unlink()
        with patch.object(PkiManager, "_run_unlocked") as signing:
            with self.assertRaises(ValueError):
                renew_crl_if_due(self.config)
            signing.assert_not_called()

    def test_malformed_or_unreadable_crl_is_not_overwritten(self):
        for output in ("", "nextUpdate=garbage", "lastUpdate=Jan  1 00:00:00 2030 GMT"):
            with patch("openvpn_addon.crl_renewal.subprocess.run", return_value=Mock(stdout=output)), \
                 patch.object(PkiManager, "_run_unlocked") as signing:
                with self.assertRaises(ValueError):
                    renew_crl_if_due(self.config)
                signing.assert_not_called()
        with patch("openvpn_addon.crl_renewal.subprocess.run", side_effect=OSError), \
             patch.object(PkiManager, "_run_unlocked") as signing:
            with self.assertRaises(OSError):
                renew_crl_if_due(self.config)
            signing.assert_not_called()
        self.assertEqual("old CRL", self.config.crl_path.read_text())

    def test_quiet_signing_suppresses_all_child_streams(self):
        with patch("openvpn_addon.pki.subprocess.run") as run:
            PkiManager(self.config)._run_unlocked("gen-crl", cwd=self.root, quiet=True)
            kwargs = run.call_args.kwargs
            for stream in ("stdout", "stderr", "stdin"):
                self.assertEqual(subprocess.DEVNULL, kwargs[stream])
            self.assertTrue(kwargs["check"])
            self.assertEqual("3650", kwargs["env"]["EASYRSA_CRL_DAYS"])

    def test_default_pki_command_does_not_create_lock_or_suppress_output(self):
        self.config.experimental_crl_auto_renew = False
        with patch("openvpn_addon.pki.subprocess.run") as run:
            PkiManager(self.config).refresh_crl()
            self.assertNotIn("stdout", run.call_args.kwargs)
        self.assertFalse((self.root / ".pki-mutation.lock").exists())

    def test_enabled_pki_commands_use_the_shared_lock(self):
        manager = PkiManager(self.config)
        with patch.object(manager, "mutation_lock") as lock, \
             patch.object(manager, "_run_unlocked") as run:
            manager.refresh_crl()
            lock.return_value.__enter__.assert_called_once()
            run.assert_called_once_with("gen-crl", cwd=self.root, cn=None, days=None)

    def test_lock_excludes_other_processes_and_releases_after_failure(self):
        probe = '''import fcntl, sys
with open(sys.argv[1], "r+") as handle:
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(42)
'''
        path = self.root / ".pki-mutation.lock"
        def probe_lock():
            return subprocess.run([sys.executable, "-c", probe, str(path)], timeout=10).returncode
        with self.assertRaises(RuntimeError):
            with PkiManager(self.config).mutation_lock():
                self.assertEqual(42, probe_lock())
                raise RuntimeError("simulated failure")
        self.assertEqual(0, probe_lock())

    def test_lock_excludes_other_threads(self):
        started, entered = threading.Event(), threading.Event()
        def writer():
            started.set()
            with PkiManager(self.config).mutation_lock():
                entered.set()
        with PkiManager(self.config).mutation_lock():
            thread = threading.Thread(target=writer, daemon=True)
            thread.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(entered.wait(0.05))
        thread.join(2)
        self.assertTrue(entered.is_set())

    def test_schedule_checks_immediately_then_hourly(self):
        schedule = CrlRenewalSchedule(self.config)
        with patch("openvpn_addon.crl_renewal.time.monotonic", side_effect=[10, 11, 3609, 3610]), \
             patch("openvpn_addon.crl_renewal.renew_crl_if_due", return_value=False) as renew:
            for _ in range(4):
                schedule.tick()
            self.assertEqual(2, renew.call_count)

    def test_failures_retry_without_logging_exception_or_child_output(self):
        schedule = CrlRenewalSchedule(self.config)
        log = io.StringIO()
        with patch("openvpn_addon.crl_renewal.time.monotonic", side_effect=[10, 20, 3610]), \
             patch("openvpn_addon.crl_renewal.renew_crl_if_due",
                   side_effect=[RuntimeError("SENSITIVE_TEST_SENTINEL"), True]) as renew, \
             contextlib.redirect_stdout(log):
            schedule.tick()
            schedule.tick()
            schedule.tick()
        self.assertEqual(2, renew.call_count)
        self.assertNotIn("SENSITIVE_TEST_SENTINEL", log.getvalue())
        self.assertIn("failed", log.getvalue())
        self.assertIn("refreshed", log.getvalue())

    def test_request_watcher_only_runs_maintenance_when_enabled(self):
        for enabled in (False, True):
            self.config.experimental_crl_auto_renew = enabled
            stop = Mock()
            stop.wait.side_effect = [False, True]
            with patch.object(RequestWatcher, "start"), \
                 patch("openvpn_addon.main.CrlRenewalSchedule") as schedule, \
                 patch("openvpn_addon.main.process_pending_requests") as requests:
                watcher = RequestWatcher(self.config, stop)
                watcher.run()
                self.assertEqual(int(enabled), schedule.call_count)
                self.assertEqual(int(enabled), schedule.return_value.tick.call_count)
                requests.assert_called_once_with(self.config)

    def test_short_lifetime_warning_reflects_selected_mode(self):
        self.config.crl_valid_days = 1
        enabled = " ".join(config_warnings(self.config))
        self.assertIn("Experimental CRL auto-renewal", enabled)
        self.assertNotIn("must be restarted before it expires", enabled)
        self.config.experimental_crl_auto_renew = False
        self.assertIn("must be restarted before it expires", " ".join(config_warnings(self.config)))


EASYRSA_TEST_BINARY = os.environ.get("EASYRSA_TEST_BINARY") or shutil.which("easyrsa")
if not EASYRSA_TEST_BINARY and Path("/usr/share/easy-rsa/easyrsa").is_file():
    EASYRSA_TEST_BINARY = "/usr/share/easy-rsa/easyrsa"


@unittest.skipUnless(EASYRSA_TEST_BINARY and shutil.which("openssl"), "EasyRSA/OpenSSL unavailable")
class RealCrlRenewalTests(unittest.TestCase):
    def test_renewal_preserves_revocation_and_keys(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(config_module, "DATA_ROOT", Path(directory)), \
             patch("openvpn_addon.pki.EASYRSA", Path(EASYRSA_TEST_BINARY)):
            config = AddonConfig(experimental_crl_auto_renew=True)
            env = {**os.environ, "EASYRSA_BATCH": "1", "EASYRSA_PKI": str(config.pki_dir),
                   "EASYRSA_REQ_CN": "Renewal test CA", "EASYRSA_ALGO": "ec",
                   "EASYRSA_CURVE": "prime256v1", "EASYRSA_CRL_DAYS": "1"}
            def easyrsa(*args):
                subprocess.run([EASYRSA_TEST_BINARY, *args], env=env, cwd=directory,
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               stdin=subprocess.DEVNULL, timeout=30)
            easyrsa("init-pki")
            easyrsa("build-ca", "nopass")
            easyrsa("build-client-full", "alice", "nopass")
            easyrsa("build-client-full", "bob", "nopass")
            cert = Path(directory) / "alice.crt"
            cert.write_bytes((config.pki_dir / "issued/alice.crt").read_bytes())
            easyrsa("revoke", "alice")
            easyrsa("gen-crl")
            index = (config.pki_dir / "index.txt").read_bytes()
            keys = {p: hashlib.sha256(p.read_bytes()).digest()
                    for p in (config.pki_dir / "private").glob("*.key")}
            old_crl = config.crl_path.read_bytes()
            # A real signing failure must leave the published CRL intact.
            ca_key = config.pki_dir / "private/ca.key"
            saved_key = config.pki_dir / "private/ca.key.saved"
            ca_key.rename(saved_key)
            try:
                with self.assertRaises(subprocess.CalledProcessError):
                    renew_crl_if_due(config)
                self.assertEqual(old_crl, config.crl_path.read_bytes())
            finally:
                saved_key.rename(ca_key)
            self.assertTrue(renew_crl_if_due(config))
            self.assertNotEqual(old_crl, config.crl_path.read_bytes())
            self.assertEqual(index, (config.pki_dir / "index.txt").read_bytes())
            for path, digest in keys.items():
                self.assertEqual(digest, hashlib.sha256(path.read_bytes()).digest())
            self.assertFalse(renew_crl_if_due(config))
            verification = subprocess.run(
                ["openssl", "verify", "-CAfile", str(config.pki_dir / "ca.crt"),
                 "-CRLfile", str(config.crl_path), "-crl_check", str(cert)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertNotEqual(0, verification.returncode)
            self.assertIn("certificate revoked", verification.stderr)
            subprocess.run(
                ["openssl", "verify", "-CAfile", str(config.pki_dir / "ca.crt"),
                 "-CRLfile", str(config.crl_path), "-crl_check",
                 str(config.pki_dir / "issued/bob.crt")],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
            )


if __name__ == "__main__":
    unittest.main()
