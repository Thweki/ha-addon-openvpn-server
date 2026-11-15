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

    def test_client_http_proxy_accepts_dns_names_and_ipv4(self):
        for host in ["proxy.example.com", "proxy", "proxy.example.com.", "192.0.2.10"]:
            for port in [1, 3128, 65535]:
                with self.subTest(host=host, port=port):
                    validate_config(AddonConfig(
                        protocol="tcp", experimental_client_http_proxy=True,
                        client_http_proxy_host=host, client_http_proxy_port=port,
                    ))

    def test_client_http_proxy_requires_tcp(self):
        with self.assertRaisesRegex(ValueError, "requires protocol: tcp"):
            validate_config(AddonConfig(
                experimental_client_http_proxy=True, client_http_proxy_host="proxy.example.com",
            ))

    def test_client_http_proxy_rejects_invalid_hosts(self):
        for host in ["", " ", "proxy host", "proxy\nverb 11", "proxy\rverb 11",
                     "proxy\t8080", "https://proxy", "user:password@proxy", "proxy:8080",
                     "proxy/path", "proxy#comment", '"proxy"', "-proxy", "proxy-",
                     "proxy..example", "a" * 64, "a." * 127, "999.1.1.1", "::1", None]:
            with self.subTest(host=host):
                with self.assertRaisesRegex(ValueError, "client_http_proxy_host"):
                    validate_config(AddonConfig(
                        protocol="tcp", experimental_client_http_proxy=True,
                        client_http_proxy_host=host,
                    ))

    def test_client_http_proxy_rejects_invalid_ports(self):
        for port in [0, -1, 65536, True, 8080.5, "8080", None]:
            with self.subTest(port=port):
                with self.assertRaisesRegex(ValueError, "client_http_proxy_port"):
                    validate_config(AddonConfig(
                        protocol="tcp", experimental_client_http_proxy=True,
                        client_http_proxy_host="proxy.example.com", client_http_proxy_port=port,
                    ))

    def test_disabled_client_http_proxy_ignores_unused_settings(self):
        validate_config(AddonConfig(
            client_http_proxy_host="not a hostname", client_http_proxy_port=0,
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
