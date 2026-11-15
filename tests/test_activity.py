import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from openvpn_addon.config import AddonConfig
from openvpn_addon.main import RequestWatcher
from openvpn_addon.pki import ClientInfo, CrlInfo
from openvpn_addon.status import collect_status, record_client_activity, status_json


NOW = 1800000000
DAY = 86400


class ActivityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for target, value in (
            ("openvpn_addon.config.DATA_ROOT", self.root),
            ("openvpn_addon.status.time.time", lambda: NOW),
            ("openvpn_addon.status.collect_health", lambda *_: []),
            ("openvpn_addon.status._network_snapshot", lambda *_: {"ip_forward": False, "out_interface": None}),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.config = AddonConfig()
        self.config.runtime_dir.mkdir()
        self.store = self.config.runtime_dir / "client_last_seen.json"
        self.pki = Mock()
        self.pki.list_clients.return_value = [ClientInfo("alice", "valid"), ClientInfo("bob", "revoked")]
        self.pki.crl_info.return_value = CrlInfo()

    def snapshot(self, names=("alice",), timestamp=NOW):
        # Existing default OpenVPN status format; no format/interval changes.
        self.config.status_path.write_text(
            "OpenVPN CLIENT LIST\nUpdated,snapshot\n"
            "Common Name,Real Address,Bytes Received,Bytes Sent,Connected Since\n"
            + "".join(f"{name},203.0.113.5:51234,100,200,Wed Oct 14 07:00:00 2026\n" for name in names)
            + "ROUTING TABLE\nEND\n",
            encoding="utf-8",
        )
        os.utime(self.config.status_path, (timestamp, timestamp))

    def clients(self):
        return {client["name"]: client for client in collect_status(self.config, self.pki)["clients"]}

    def test_observation_survives_offline_and_new_config_instance(self):
        self.snapshot(timestamp=NOW - 3 * DAY - 123)
        record_client_activity(self.config)
        self.snapshot(names=())
        self.config = AddonConfig()
        alice = self.clients()["alice"]
        self.assertFalse(alice["online"])
        self.assertEqual(3, alice["inactive_days"])
        self.assertEqual(NOW - 3 * DAY - 123, alice["last_seen"])
        self.assertEqual(0o600, self.store.stat().st_mode & 0o777)
        self.assertEqual({"alice": NOW - 3 * DAY - 123}, json.loads(self.store.read_text()))

    def test_unknown_is_null_and_reading_status_does_not_write(self):
        self.snapshot()
        for client in self.clients().values():
            self.assertIsNone(client["last_seen"])
            self.assertIsNone(client["inactive_days"])
        self.assertFalse(self.store.exists())

    def test_whole_day_boundaries_and_revoked_clients_in_status_json(self):
        for elapsed, expected in ((0, 0), (DAY - 1, 0), (DAY, 1), (2 * DAY - 1, 1), (12 * DAY, 12)):
            with self.subTest(elapsed=elapsed):
                self.store.write_text(json.dumps({"alice": NOW - elapsed, "bob": NOW - elapsed}))
                payload = json.loads(status_json(self.config, self.pki))
                self.assertEqual([expected, expected], [c["inactive_days"] for c in payload["clients"]])
                self.assertEqual("revoked", payload["clients"][1]["status"])

    def test_stale_snapshot_does_not_reset_age_or_rewrite_store(self):
        self.snapshot(timestamp=NOW - 4 * DAY)
        record_client_activity(self.config)
        with patch("openvpn_addon.status.os.replace") as replace:
            record_client_activity(self.config)
            replace.assert_not_called()
        self.assertEqual(4, self.clients()["alice"]["inactive_days"])

    def test_reconnection_updates_only_seen_clients_and_never_moves_backwards(self):
        self.snapshot(names=("alice", "bob"), timestamp=NOW - 10 * DAY)
        record_client_activity(self.config)
        self.snapshot(timestamp=NOW)
        record_client_activity(self.config)
        self.snapshot(timestamp=NOW - DAY)
        record_client_activity(self.config)
        self.assertEqual({"alice": NOW, "bob": NOW - 10 * DAY}, json.loads(self.store.read_text()))

    def test_missing_empty_or_future_snapshot_does_not_fabricate_activity(self):
        record_client_activity(self.config)
        self.snapshot(names=())
        record_client_activity(self.config)
        self.snapshot(timestamp=NOW + DAY)
        record_client_activity(self.config)
        self.assertFalse(self.store.exists())

    def test_invalid_store_is_unknown_and_recovers_on_observation(self):
        for contents in (b"{broken", b"[]", b"null", b"\xff"):
            with self.subTest(contents=contents):
                self.store.write_bytes(contents)
                self.assertIsNone(self.clients()["alice"]["inactive_days"])
                self.snapshot()
                record_client_activity(self.config)
                self.assertEqual(0, self.clients()["alice"]["inactive_days"])

    def test_invalid_timestamps_are_unknown(self):
        for timestamp in (True, "123", {}, -1, 0, 1.5, float("nan"), float("inf"), NOW + DAY):
            with self.subTest(timestamp=timestamp):
                self.store.write_text(json.dumps({"alice": timestamp, "bob": NOW - DAY}))
                clients = self.clients()
                self.assertIsNone(clients["alice"]["inactive_days"])
                self.assertEqual(1, clients["bob"]["inactive_days"])

    def test_snapshot_rewritten_during_read_is_retried_later(self):
        self.snapshot(timestamp=NOW - DAY)

        def rewrite(_):
            self.snapshot(timestamp=NOW)
            return {"alice": {}}

        with patch("openvpn_addon.status._parse_status", side_effect=rewrite):
            record_client_activity(self.config)
        self.assertFalse(self.store.exists())
        record_client_activity(self.config)
        self.assertEqual(0, self.clients()["alice"]["inactive_days"])

    def test_failed_atomic_write_preserves_previous_store_and_cleans_temporary_file(self):
        self.store.write_text(json.dumps({"alice": NOW - DAY}))
        self.snapshot()
        with patch("openvpn_addon.status.os.replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                record_client_activity(self.config)
        self.assertEqual({"alice": NOW - DAY}, json.loads(self.store.read_text()))
        self.assertEqual([], list(self.config.runtime_dir.glob(".client_last_seen-*")))

    def test_background_watcher_records_without_panel_and_survives_request_errors(self):
        self.snapshot(timestamp=NOW - DAY)
        stop = Mock(spec=threading.Event)
        stop.wait.side_effect = [False, True]
        with patch.object(RequestWatcher, "start"), patch(
            "openvpn_addon.main.process_pending_requests", side_effect=OSError("request error")
        ), patch("builtins.print"):
            RequestWatcher(self.config, stop).run()
        self.assertEqual(1, self.clients()["alice"]["inactive_days"])

    def test_activity_failure_does_not_stop_request_watcher(self):
        stop = Mock(spec=threading.Event)
        stop.wait.side_effect = [False, False, True]
        with patch.object(RequestWatcher, "start"), patch(
            "openvpn_addon.main.process_pending_requests"
        ) as requests, patch(
            "openvpn_addon.main.record_client_activity", side_effect=OSError("disk unavailable")
        ) as record, patch("builtins.print"):
            RequestWatcher(self.config, stop).run()
        self.assertEqual(2, requests.call_count)
        self.assertEqual(2, record.call_count)


if __name__ == "__main__":
    unittest.main()
