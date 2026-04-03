import json
import tempfile
import unittest
from pathlib import Path

from openvpn_addon.config import load_config


class ConfigLoadingTests(unittest.TestCase):
    def test_loads_new_string_fields(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "options.json"
            path.write_text(
                json.dumps(
                    {
                        "dns": "1.1.1.1, 8.8.8.8",
                        "lan_networks": "192.168.1.0/24, 192.168.2.0/24",
                        "extra_routes": "10.0.0.0/24",
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(path)
            self.assertEqual(["1.1.1.1", "8.8.8.8"], config.dns_servers)
            self.assertEqual(["192.168.1.0/24", "192.168.2.0/24"], config.local_networks)
            self.assertEqual(["10.0.0.0/24"], config.push_routes)

    def test_loads_legacy_list_fields(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "options.json"
            path.write_text(
                json.dumps(
                    {
                        "dns_servers": ["1.1.1.1", "8.8.8.8"],
                        "local_networks": ["192.168.1.0/24"],
                        "push_routes": ["10.0.0.0/24"],
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(path)
            self.assertEqual(["1.1.1.1", "8.8.8.8"], config.dns_servers)
            self.assertEqual(["192.168.1.0/24"], config.local_networks)
            self.assertEqual(["10.0.0.0/24"], config.push_routes)


if __name__ == "__main__":
    unittest.main()
