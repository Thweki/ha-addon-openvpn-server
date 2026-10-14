import json
import tempfile
import unittest
from pathlib import Path

import openvpn_addon.config as config_module
from openvpn_addon.config import AddonConfig
from openvpn_addon.status import collect_status, status_json


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
