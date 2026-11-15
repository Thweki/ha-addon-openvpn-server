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

    def test_client_retry_accepts_modes_and_boundaries(self):
        for mode in ("none", "nointeract", "interact"):
            for delay, cap, attempts in ((1, 1, 0), (5, 60, 12), (86400, 86400, 65535)):
                with self.subTest(mode=mode, delay=delay, cap=cap, attempts=attempts):
                    validate_config(AddonConfig(
                        experimental_client_retry=True,
                        client_auth_retry=mode,
                        client_connect_retry_seconds=delay,
                        client_connect_retry_max_seconds=cap,
                        client_connect_retry_max_attempts=attempts,
                    ))

    def test_client_retry_rejects_invalid_values_even_when_disabled(self):
        invalid = {
            "client_auth_retry": ("", "retry", "none\nscript-security 3", None, []),
            "client_connect_retry_seconds": (0, -1, 86401),
            "client_connect_retry_max_seconds": (0, -1, 86401),
            "client_connect_retry_max_attempts": (-1, 65536),
        }
        for enabled in (False, True):
            for field, values in invalid.items():
                for value in values:
                    with self.subTest(enabled=enabled, field=field, value=value):
                        config = AddonConfig(experimental_client_retry=enabled)
                        setattr(config, field, value)
                        with self.assertRaisesRegex(ValueError, field):
                            validate_config(config)

    def test_client_retry_rejects_cap_below_initial_delay(self):
        with self.assertRaisesRegex(ValueError, "client_connect_retry_max_seconds"):
            validate_config(AddonConfig(
                client_connect_retry_seconds=60,
                client_connect_retry_max_seconds=5,
            ))

    def test_reject_overlapping_vpn_and_local_networks(self):
        config = AddonConfig(
            server_host="192.168.1.6",
            vpn_subnet="192.168.1.0",
            vpn_netmask="255.255.255.0",
            local_networks=["192.168.1.0/24"],
        )
        with self.assertRaises(ValueError):
            validate_config(config)

    def test_reject_non_positive_crl_days(self):
        config = AddonConfig(server_host="192.168.1.6", crl_valid_days=0)
        with self.assertRaises(ValueError):
            validate_config(config)

    def test_warns_when_nat_is_off_but_lan_networks_are_configured(self):
        # The exact misconfiguration that silently broke LAN access.
        config = AddonConfig(
            server_host="192.168.1.6",
            public_endpoint="vpn.example.com",
            local_networks=["192.168.1.0/24"],
            nat_enabled=False,
        )
        warnings = config_warnings(config)
        self.assertTrue(any("nat_enabled is off but lan_networks" in item for item in warnings), warnings)

    def test_warns_when_nat_is_on_without_lan_networks(self):
        config = AddonConfig(server_host="192.168.1.6", public_endpoint="vpn.example.com", local_networks=[])
        warnings = config_warnings(config)
        self.assertTrue(any("local_networks is empty" in item for item in warnings), warnings)

    def test_warns_about_single_host_lan_entry(self):
        config = AddonConfig(
            server_host="192.168.1.6",
            public_endpoint="vpn.example.com",
            local_networks=["192.168.1.5"],
        )
        warnings = config_warnings(config)
        self.assertTrue(any("is not a subnet" in item for item in warnings), warnings)

    def test_warns_that_shared_certificates_cannot_use_fixed_ips(self):
        config = AddonConfig(
            server_host="192.168.1.6",
            public_endpoint="vpn.example.com",
            local_networks=["192.168.1.0/24"],
            duplicate_cn=True,
            fixed_client_ips=True,
        )
        warnings = config_warnings(config)
        self.assertTrue(any("fixed_client_ips is enabled but duplicate_cn" in item for item in warnings), warnings)

    def test_warns_about_unknown_options(self):
        config = AddonConfig(server_host="192.168.1.6", unknown_options=["legacy_thing"])
        warnings = config_warnings(config)
        self.assertTrue(any("legacy_thing" in item for item in warnings), warnings)

    def test_warns_about_short_crl_lifetime(self):
        config = AddonConfig(server_host="192.168.1.6", crl_valid_days=30)
        warnings = config_warnings(config)
        self.assertTrue(any("crl_valid_days" in item for item in warnings), warnings)

    def test_default_config_only_warns_about_missing_endpoint_and_routes(self):
        config = AddonConfig(server_host="192.168.1.6")
        warnings = config_warnings(config)
        self.assertEqual(2, len(warnings), warnings)
        self.assertIn("public_endpoint is empty", warnings[0])


if __name__ == "__main__":
    unittest.main()
