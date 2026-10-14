import json
import tempfile
import unittest
from pathlib import Path

from openvpn_addon.config import load_config


class ConfigLoadingTests(unittest.TestCase):
    def load(self, payload):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "options.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return load_config(path)

    def test_loads_new_string_fields(self):
        config = self.load(
            {
                "dns": "1.1.1.1, 8.8.8.8",
                "lan_networks": "192.168.1.0/24, 192.168.2.0/24",
                "extra_routes": "10.0.0.0/24",
            }
        )
        self.assertEqual(["1.1.1.1", "8.8.8.8"], config.dns_servers)
        self.assertEqual(["192.168.1.0/24", "192.168.2.0/24"], config.local_networks)
        self.assertEqual(["10.0.0.0/24"], config.push_routes)

    def test_loads_legacy_list_fields(self):
        config = self.load(
            {
                "dns_servers": ["1.1.1.1", "8.8.8.8"],
                "local_networks": ["192.168.1.0/24"],
                "push_routes": ["10.0.0.0/24"],
            }
        )
        self.assertEqual(["1.1.1.1", "8.8.8.8"], config.dns_servers)
        self.assertEqual(["192.168.1.0/24"], config.local_networks)
        self.assertEqual(["10.0.0.0/24"], config.push_routes)

    def test_new_options_default_safely(self):
        config = self.load({})
        self.assertFalse(config.publish_profiles_to_share)
        self.assertTrue(config.fixed_client_ips)
        self.assertEqual(3650, config.crl_valid_days)
        self.assertEqual([], config.unknown_options)

    def test_unknown_options_are_dropped_and_reported(self):
        config = self.load({"nat_enabled": True, "legacy_knob": "x", "another": 1})
        self.assertEqual(["another", "legacy_knob"], config.unknown_options)

    def test_scalar_types_are_coerced(self):
        config = self.load(
            {
                "nat_enabled": "false",
                "duplicate_cn": "true",
                "crl_valid_days": "100",
                "listen_port": 1194,
                "publish_profiles_to_share": 0,
            }
        )
        self.assertIs(False, config.nat_enabled)
        self.assertIs(True, config.duplicate_cn)
        self.assertEqual(100, config.crl_valid_days)
        self.assertIs(False, config.publish_profiles_to_share)

    def test_non_integer_value_is_rejected(self):
        with self.assertRaises(ValueError):
            self.load({"crl_valid_days": "many"})

    def test_non_object_options_are_rejected(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "options.json"
            path.write_text("[1, 2]", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
