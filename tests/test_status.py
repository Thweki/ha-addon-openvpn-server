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
        config_module.DATA_ROOT = root
        config_module.SHARE_ROOT = root / "share"
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
        self.assertEqual(["public_port differs from listen_port; ensure your router or firewall forwards the external port to the local listening port."], data["warnings"])

    def test_status_json_is_valid_json(self):
        payload = json.loads(status_json(self.config))
        self.assertEqual("udp", payload["summary"]["protocol"])


if __name__ == "__main__":
    unittest.main()
