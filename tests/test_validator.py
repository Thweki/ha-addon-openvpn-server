import unittest

from openvpn_addon.config import AddonConfig
from openvpn_addon.validator import config_warnings, validate_client_name, validate_config


class ValidatorTests(unittest.TestCase):
    def test_validate_default_config(self):
        config = AddonConfig(server_host="192.168.1.6")
        validate_config(config)

    def test_validate_bad_client_name(self):
        with self.assertRaises(ValueError):
            validate_client_name("bad name")

    def test_reject_overlapping_vpn_and_local_networks(self):
        config = AddonConfig(
            server_host="192.168.1.6",
            vpn_subnet="192.168.1.0",
            vpn_netmask="255.255.255.0",
            local_networks=["192.168.1.0/24"],
        )
        with self.assertRaises(ValueError):
            validate_config(config)

    def test_config_warnings_cover_common_misconfigurations(self):
        config = AddonConfig(
            public_endpoint="",
            public_port=443,
            listen_port=1194,
            nat_enabled=True,
            local_networks=[],
            redirect_gateway=True,
            duplicate_cn=True,
        )
        warnings = config_warnings(config)
        self.assertEqual(5, len(warnings))
        self.assertIn("public_endpoint is empty", warnings[0])


if __name__ == "__main__":
    unittest.main()
