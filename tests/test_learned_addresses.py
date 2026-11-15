import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from openvpn_addon.config import AddonConfig
from openvpn_addon.main import RequestWatcher
from openvpn_addon.status import collect_status, learn_client_addresses, load_learned_addresses
from openvpn_addon.webui import PAGE


class LearnedAddressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        root_patch = patch("openvpn_addon.config.DATA_ROOT", root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.config = AddonConfig()
        self.config.runtime_dir.mkdir()
        self.config.pki_dir.mkdir()
        self.index = self.config.pki_dir / "index.txt"
        self.index.write_text("V\t300101000000Z\t\t01\tunknown\t/CN=alice\n", encoding="utf-8")

    def snapshot(self, address="203.0.113.5:51234", timestamp=1800000000, name="alice", complete=True):
        rows = f"{name},{address},100,200,Wed Oct 14 07:00:00 2026\n" if address else ""
        self.config.status_path.write_text(
            "OpenVPN CLIENT LIST\nUpdated,Wed Oct 14 08:00:00 2026\n"
            "Common Name,Real Address,Bytes Received,Bytes Sent,Connected Since\n"
            + rows + "ROUTING TABLE\nVirtual Address,Common Name,Real Address,Last Ref\n"
            + ("GLOBAL STATS\nMax bcast/mcast queue length,0\nEND\n" if complete else ""),
            encoding="utf-8",
        )
        os.utime(self.config.status_path, (timestamp, timestamp))

    def test_learns_endpoint_and_snapshot_time_with_private_permissions(self):
        self.snapshot()
        learn_client_addresses(self.config)
        self.assertEqual({"alice": {
            "address": "203.0.113.5:51234", "observed_at": "2027-01-15T08:00:00+00:00",
        }}, load_learned_addresses(self.config))
        self.assertEqual(0o600, self.config.learned_addresses_path.stat().st_mode & 0o777)

    def test_retains_after_disconnect_and_new_config_instance(self):
        self.snapshot()
        learn_client_addresses(self.config)
        expected = load_learned_addresses(self.config)
        self.snapshot(address=None, timestamp=1800000030)
        learn_client_addresses(AddonConfig())
        self.assertEqual(expected, load_learned_addresses(AddonConfig()))
        data = collect_status(AddonConfig())
        client = data["clients"][0]
        self.assertFalse(client["online"])
        self.assertIsNone(client["real_address"])
        self.assertEqual(expected["alice"]["address"], client["last_real_address"])
        self.assertEqual(expected["alice"]["observed_at"], client["last_real_address_at"])

    def test_reconnect_replaces_address_without_keeping_history(self):
        self.snapshot()
        learn_client_addresses(self.config)
        self.snapshot("[2001:db8::1]:43210", timestamp=1800000030)
        learn_client_addresses(self.config)
        self.assertEqual({"alice": {
            "address": "[2001:db8::1]:43210", "observed_at": "2027-01-15T08:00:30+00:00",
        }}, load_learned_addresses(self.config))
        self.snapshot(timestamp=1800000000)
        learn_client_addresses(self.config)
        self.assertEqual("[2001:db8::1]:43210", load_learned_addresses(self.config)["alice"]["address"])

    def test_unchanged_snapshot_does_not_write_or_advance_observation_time(self):
        self.snapshot()
        learn_client_addresses(self.config)
        with patch("openvpn_addon.status.os.replace") as replace:
            learn_client_addresses(self.config)
        replace.assert_not_called()

    def test_missing_partial_and_invalid_endpoints_preserve_previous_observation(self):
        self.snapshot()
        learn_client_addresses(self.config)
        expected = load_learned_addresses(self.config)
        self.config.status_path.unlink()
        learn_client_addresses(self.config)
        self.assertEqual(expected, load_learned_addresses(self.config))
        self.snapshot("198.51.100.2:443", timestamp=1800000030, complete=False)
        learn_client_addresses(self.config)
        self.assertEqual(expected, load_learned_addresses(self.config))
        for value in (
            "<img src=x onerror=alert(1)>:443", "203.0.113.1:99999", "host:443",
            "", "203.0.113.1:0", "[2001:db8::1:443", "2001:db8::1]:443",
        ):
            with self.subTest(value=value):
                self.snapshot(value, timestamp=1800000030)
                learn_client_addresses(self.config)
                self.assertEqual(expected, load_learned_addresses(self.config))

    def test_snapshot_changed_during_read_is_not_learned(self):
        self.snapshot()
        original_stat = Path.stat
        counter = 0

        def changing_stat(path, *args, **kwargs):
            nonlocal counter
            if path == self.config.status_path:
                counter += 1
                if counter == 2:
                    os.utime(path, (1800000030, 1800000030))
            return original_stat(path, *args, **kwargs)

        with patch.object(Path, "stat", changing_stat):
            learn_client_addresses(self.config)
        self.assertFalse(self.config.learned_addresses_path.exists())

    def test_new_snapshot_refreshes_same_endpoint_observation_time(self):
        self.snapshot()
        learn_client_addresses(self.config)
        self.snapshot(timestamp=1800000030)
        learn_client_addresses(self.config)
        self.assertEqual("2027-01-15T08:00:30+00:00", load_learned_addresses(self.config)["alice"]["observed_at"])

    def test_only_issued_clients_are_retained_and_removed_clients_are_pruned(self):
        self.snapshot(name="unknown")
        learn_client_addresses(self.config)
        self.assertEqual({}, load_learned_addresses(self.config))
        self.snapshot()
        learn_client_addresses(self.config)
        self.index.write_text("", encoding="utf-8")
        learn_client_addresses(self.config)
        self.assertEqual({}, load_learned_addresses(self.config))

    def test_never_seen_client_has_no_endpoint_and_status_does_not_write(self):
        data = collect_status(self.config)
        self.assertIsNone(data["clients"][0]["last_real_address"])
        self.assertIsNone(data["clients"][0]["last_real_address_at"])
        self.assertFalse(self.config.learned_addresses_path.exists())

    def test_corrupt_cache_is_ignored_and_can_be_rebuilt(self):
        for payload in (b"{", b"[]", b"\xff", b'{"alice":null}', b'{"alice":{"address":"203.0.113.1:443","observed_at":"bad"}}'):
            with self.subTest(payload=payload):
                self.config.learned_addresses_path.write_bytes(payload)
                self.assertEqual({}, load_learned_addresses(self.config))
                self.snapshot()
                learn_client_addresses(self.config)
                self.assertIn("alice", load_learned_addresses(self.config))

    def test_failed_atomic_replace_keeps_previous_cache_and_cleans_temporary_file(self):
        self.snapshot()
        learn_client_addresses(self.config)
        expected = load_learned_addresses(self.config)
        before = set(self.config.runtime_dir.iterdir())
        self.snapshot("198.51.100.2:443", timestamp=1800000030)
        with patch("openvpn_addon.status.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                learn_client_addresses(self.config)
        self.assertEqual(expected, load_learned_addresses(self.config))
        self.assertEqual(before, set(self.config.runtime_dir.iterdir()))

    def test_watcher_learns_without_panel_and_survives_collection_failure(self):
        self.snapshot()
        event = Mock()
        event.wait.side_effect = [False, False, True]
        with patch.object(RequestWatcher, "start"):
            watcher = RequestWatcher(self.config, event)
        with patch("openvpn_addon.main.process_pending_requests") as requests, patch(
            "openvpn_addon.main.learn_client_addresses", side_effect=[OSError("unavailable"), None]
        ) as learn, contextlib.redirect_stdout(io.StringIO()):
            watcher.run()
        self.assertEqual(2, requests.call_count)
        self.assertEqual(2, learn.call_count)
        event.wait.side_effect = [False, True]
        with patch("openvpn_addon.main.process_pending_requests"):
            watcher.run()
        self.assertEqual("203.0.113.5:51234", load_learned_addresses(self.config)["alice"]["address"])


@unittest.skipUnless(shutil.which("node"), "Node.js is required to execute panel JavaScript")
class LearnedAddressPanelTests(unittest.TestCase):
    def render_clients(self, clients):
        # Execute the actual page script; no browser or listening socket required.
        payload = {
            "summary": {}, "health": {"status": "ok", "checks": []},
            "routes": [], "crl": {}, "clients": clients,
        }
        setup = """
const elements = {};
global.document = {
  getElementById: (id) => elements[id] ||= {},
  addEventListener: () => {}
};
global.fetch = async () => ({ok: true, text: async () => JSON.stringify(PAYLOAD)});
""".replace("PAYLOAD", json.dumps(payload))
        script = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
        result = subprocess.run(
            ["node", "-e", setup + script + '\nsetImmediate(() => console.log(elements.clients.innerHTML));'],
            capture_output=True, text=True, check=True,
        )
        return result.stdout

    def test_panel_displays_offline_endpoint_timestamp_and_unknown_placeholder(self):
        self.assertIn("Last public endpoint", PAGE)
        html = self.render_clients([
            {"name": "alice", "online": False, "last_real_address": "[2001:db8::1]:443",
             "last_real_address_at": "2027-01-15T08:00:00+00:00"},
            {"name": "bob", "online": False},
        ])
        self.assertIn("[2001:db8::1]:443", html)
        self.assertIn("2027-01-15T08:00:00+00:00", html)
        self.assertIn("<td>no</td>", html)
        self.assertIn("<td>—</td>", html)

    def test_panel_escapes_metadata_and_empty_table_uses_six_columns(self):
        html = self.render_clients([{
            "name": "alice", "last_real_address": '<img src=x onerror="bad">',
            "last_real_address_at": "<script>bad</script>",
        }])
        self.assertNotIn("<img", html)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;img", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn('colspan="6"', self.render_clients([]))


if __name__ == "__main__":
    unittest.main()
