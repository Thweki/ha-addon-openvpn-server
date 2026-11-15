import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import openvpn_addon.config as config_module
from openvpn_addon.config import AddonConfig
from openvpn_addon.status import _parse_status, collect_status, status_json


class StatusFileTests(unittest.TestCase):
    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.path = Path(tempdir.name) / "status.log"

    def test_missing_and_empty_files(self):
        self.assertEqual({}, _parse_status(self.path))
        self.path.touch()
        self.assertEqual({}, _parse_status(self.path))

    def test_line_endings_and_unterminated_last_line(self):
        for newline in (b"\n", b"\r\n", b"\r"):
            with self.subTest(newline=newline):
                self.path.write_bytes(newline.join([
                    b"OpenVPN CLIENT LIST",
                    b"Common Name,Real Address,Virtual Address,Bytes Received,Bytes Sent,Connected Since",
                    b"",
                    b"incomplete,row",
                    b"alice,203.0.113.5:51234,10.8.0.2,100,200,Wed Oct 14 07:00:00 2026",
                    b"bob,203.0.113.6:51234,10.8.0.3,300,400",
                ]))
                self.assertEqual({
                    "alice": {
                        "common_name": "alice",
                        "real_address": "203.0.113.5:51234",
                        "virtual_address": "10.8.0.2",
                        "bytes_received": "100",
                        "bytes_sent": "200",
                        "connected_since": "Wed Oct 14 07:00:00 2026",
                    },
                    "bob": {
                        "common_name": "bob",
                        "real_address": "203.0.113.6:51234",
                        "virtual_address": "10.8.0.3",
                        "bytes_received": "300",
                        "bytes_sent": "400",
                        "connected_since": "",
                    },
                }, _parse_status(self.path))

    def test_invalid_utf8_is_replaced(self):
        self.path.write_bytes(b"Common Name,\nalice,remote,10.8.0.2,100,200,invalid\xff\n")
        self.assertEqual("invalid\ufffd", _parse_status(self.path)["alice"]["connected_since"])

    def test_streams_only_through_client_section(self):
        class StreamingOnlyFile(io.StringIO):
            def read(self, *args, **kwargs):
                raise AssertionError("status file must not be read in full")

            def readlines(self, *args, **kwargs):
                raise AssertionError("status lines must not be collected in full")

            def __next__(self):
                line = super().__next__()
                if line.startswith("unused route"):
                    raise AssertionError("routing entries must not be consumed")
                return line

        self.path.touch()
        client_count = 10000
        stream = StreamingOnlyFile(
            "Common Name,\n"
            + "".join(f"client{i},remote,10.8.0.2,100,200,since\n" for i in range(client_count))
            + "ROUTING TABLE\n"
            + "unused route\n" * client_count
        )
        with patch.object(Path, "open", return_value=stream):
            sessions = _parse_status(self.path)
        self.assertTrue(stream.closed)
        self.assertEqual(client_count, len(sessions))
        self.assertEqual("since", sessions["client9999"]["connected_since"])


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.original_root = config_module.DATA_ROOT
        self.original_share = config_module.SHARE_ROOT
        config_module.DATA_ROOT = root
        config_module.SHARE_ROOT = root / "share"
        self.addCleanup(setattr, config_module, "DATA_ROOT", self.original_root)
        self.addCleanup(setattr, config_module, "SHARE_ROOT", self.original_share)

        self.config = AddonConfig(
            server_host="0.0.0.0",
            public_endpoint="vpn.example.com",
            listen_port=1194,
            public_port=443,
            protocol="udp",
            local_networks=["192.168.1.0/24"],
        )
        self.config.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.config.share_exports_dir.mkdir(parents=True, exist_ok=True)
        self.config.share_requests_dir.mkdir(parents=True, exist_ok=True)
        self.config.share_results_dir.mkdir(parents=True, exist_ok=True)

    def test_collect_status_includes_summary_routes_and_warnings(self):
        data = collect_status(self.config)
        self.assertEqual("0.0.0.0:1194", data["summary"]["listen_address"])
        self.assertEqual("vpn.example.com:443", data["summary"]["public_address"])
        self.assertEqual(["192.168.1.0/24"], data["routes"])
        self.assertEqual(
            ["public_port differs from listen_port; ensure your router or firewall forwards the external port to the local listening port."],
            data["warnings"],
        )

    def test_collect_status_includes_health_and_networking(self):
        data = collect_status(self.config)
        self.assertIn(data["health"]["status"], {"ok", "warn", "error"})
        names = [item["name"] for item in data["health"]["checks"]]
        self.assertIn("lan_forwarding", names)
        self.assertIn("ip_forward", names)
        self.assertIn("crl", names)
        self.assertEqual("10.8.0.0/24", data["networking"]["vpn_network"])
        self.assertIn("next_update", data["crl"])

    def test_clients_merge_certificates_with_live_sessions(self):
        self.config.status_path.write_text(
            "OpenVPN CLIENT LIST\n"
            "Updated,Wed Oct 14 08:00:00 2026\n"
            "Common Name,Real Address,Bytes Received,Bytes Sent,Connected Since\n"
            "alice,203.0.113.5:51234,100,200,Wed Oct 14 07:00:00 2026\n"
            "ROUTING TABLE\n",
            encoding="utf-8",
        )
        data = collect_status(self.config)
        self.assertEqual(["alice"], data["clients_online"])
        self.assertEqual([], data["clients"])

    def test_status_json_is_valid_json(self):
        payload = json.loads(status_json(self.config))
        self.assertEqual("udp", payload["summary"]["protocol"])


if __name__ == "__main__":
    unittest.main()
