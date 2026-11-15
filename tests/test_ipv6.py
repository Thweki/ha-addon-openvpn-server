import ipaddress
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openvpn_addon import config as config_module
from openvpn_addon import renderer
from openvpn_addon.config import AddonConfig, load_config
from openvpn_addon.pki import PkiManager
from openvpn_addon.validator import config_warnings, validate_config


ROOT = Path(__file__).resolve().parent.parent


class IPv6Tests(unittest.TestCase):
    def load(self, payload):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "options.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return load_config(path)

    def render(self, config):
        with patch.object(renderer, "TEMPLATES_DIR", ROOT / "addon/templates"):
            return renderer.render_server_config(config)

    def test_existing_options_keep_ipv6_disabled(self):
        config = self.load({})
        self.assertIs(config.experimental_ipv6, False)
        validate_config(config)
        self.assertNotIn("ipv6", self.render(config))
        self.assertFalse(any("experimental_ipv6" in w for w in config_warnings(config)))

    def test_false_strings_do_not_enable_ipv6(self):
        for value in (False, "false", "off", "0", 0):
            with self.subTest(value=value):
                config = self.load({"experimental_ipv6": value})
                self.assertIs(config.experimental_ipv6, False)
                self.assertNotIn("server-ipv6", self.render(config))

    def test_enabled_options_reach_shipped_template(self):
        for protocol in ("udp", "tcp"):
            with self.subTest(protocol=protocol):
                config = self.load({
                    "experimental_ipv6": "true",
                    "vpn_ipv6_subnet": "fd42:1234:5678:0001::/64",
                    "protocol": protocol,
                })
                validate_config(config)
                self.assertEqual([], config.unknown_options)
                output = self.render(config)
                self.assertEqual(1, output.count("server-ipv6 fd42:1234:5678:1::/64\n"))
                self.assertIn("server 10.8.0.0 255.255.255.0\n", output)
                self.assertIn(f"proto {protocol}\n", output)
                self.assertIn("dev tun0\n", output)

    def test_enabled_default_subnet_is_valid(self):
        config = AddonConfig(experimental_ipv6=True)
        validate_config(config)
        self.assertIn("server-ipv6 fd42:6f70:656e:8::/64", self.render(config))

    def test_accepts_unique_local_and_global_unicast_networks(self):
        for subnet in ("fdab:1234:5678:1::/64", "2001:db8:1234:1::/64"):
            with self.subTest(subnet=subnet):
                validate_config(AddonConfig(experimental_ipv6=True, vpn_ipv6_subnet=subnet))

    def test_rejects_invalid_or_unsuitable_subnets(self):
        for subnet in (
            "", None, 64, [], "10.8.0.0/24", "fd42::1/64", "fd42::",
            "fd42::/63", "fd42::/65", "fd42::/128", "fe80::/64",
            "ff02::/64", "::/64", "::ffff:0:0/96",
            "fd42::/64\npush redirect-gateway",
        ):
            with self.subTest(subnet=subnet):
                with self.assertRaisesRegex(ValueError, "vpn_ipv6_subnet"):
                    validate_config(AddonConfig(experimental_ipv6=True, vpn_ipv6_subnet=subnet))

    def test_enabled_ipv6_requires_tun(self):
        for device in ("tap", "tun1", "tun\nverb 9"):
            with self.subTest(device=device):
                with self.assertRaisesRegex(ValueError, "device.*tun"):
                    validate_config(AddonConfig(experimental_ipv6=True, device=device))

    def test_disabled_feature_ignores_retained_ipv6_options(self):
        config = AddonConfig(experimental_ipv6=False, vpn_ipv6_subnet="invalid", device="tap")
        validate_config(config)
        self.assertNotIn("ipv6", self.render(config))

    def test_toggling_off_restores_ipv4_config(self):
        config = AddonConfig(local_networks=["192.168.1.0/24"], redirect_gateway=True)
        original = self.render(config)
        config.experimental_ipv6 = True
        output = self.render(config)
        self.assertIn('push "redirect-gateway def1 bypass-dhcp"', output)
        self.assertNotIn("redirect-gateway ipv6", output)
        self.assertNotIn("route-ipv6", output)
        without_ipv6 = [
            line if not line.startswith("server-ipv6 ") else ""
            for line in output.splitlines()
        ]
        self.assertEqual(original.splitlines(), without_ipv6)
        config.experimental_ipv6 = False
        self.assertEqual(original, self.render(config))

    def test_enabled_warning_explains_scope(self):
        warnings = config_warnings(AddonConfig(experimental_ipv6=True))
        warning = next(w for w in warnings if "experimental_ipv6" in w)
        self.assertIn("IPv4 only", warning)
        self.assertIn("IPv6 internet", warning)

    def test_existing_client_profiles_can_receive_ipv6_pushes(self):
        config = AddonConfig(public_endpoint="vpn.example.com")
        with (
            patch.object(renderer, "TEMPLATES_DIR", ROOT / "addon/templates"),
            patch.object(renderer, "_read_file", return_value="TEST DATA"),
            patch.object(renderer, "_read_cert_body", return_value="TEST CERTIFICATE"),
        ):
            original = renderer.render_client_config(config, "alice")
            config.experimental_ipv6 = True
            self.assertEqual(original, renderer.render_client_config(config, "alice"))
        self.assertIn("client", original.splitlines())
        self.assertIn("dev tun", original.splitlines())
        self.assertNotIn("pull-filter", original)

    def test_addon_exposes_opt_in_and_translated_fields(self):
        manifest = (ROOT / "addon/config.yaml").read_text(encoding="utf-8")
        self.assertIn("  experimental_ipv6: false\n", manifest)
        self.assertIn("  experimental_ipv6: bool?\n", manifest)
        self.assertIn('  vpn_ipv6_subnet: "fd42:6f70:656e:8::/64"\n', manifest)
        self.assertIn("  vpn_ipv6_subnet: str?\n", manifest)
        for language in ("en", "zh-Hans"):
            translations = json.loads((ROOT / f"addon/translations/{language}.json").read_text(encoding="utf-8"))
            for option in ("experimental_ipv6", "vpn_ipv6_subnet"):
                self.assertTrue(translations["configuration"][option]["description"])


class IPv6AssignmentTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root_patch = patch.object(config_module, "DATA_ROOT", Path(directory.name))
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.config = AddonConfig(experimental_ipv6=True)
        self.pki = PkiManager(self.config)

    def ccd(self, name):
        return (self.config.ccd_dir / name).read_text(encoding="utf-8")

    def test_fixed_ipv4_clients_receive_distinct_ipv6_outside_dynamic_pool(self):
        assignments = self.pki.ensure_client_ips(["alice", "bob"])
        network = ipaddress.IPv6Network(self.config.vpn_ipv6_subnet)
        addresses = set()
        for name, ipv4 in assignments.items():
            ipv4_line, ipv6_line = self.ccd(name).splitlines()
            self.assertEqual(f"ifconfig-push {ipv4} 255.255.255.0", ipv4_line)
            directive, local, remote = ipv6_line.split()
            self.assertEqual("ifconfig-ipv6-push", directive)
            interface = ipaddress.IPv6Interface(local)
            self.assertEqual(network, interface.network)
            self.assertEqual(str(network.network_address + 1), remote)
            offset = int(interface.ip) - int(network.network_address)
            self.assertGreater(offset, 0x1ffff)
            self.assertEqual(int(ipaddress.IPv4Address(ipv4)), offset - (1 << 32))
            addresses.add(interface.ip)
        self.assertEqual(2, len(addresses))

    def test_fixed_assignments_survive_resync_and_restart(self):
        self.pki.ensure_client_ips(["alice", "bob"])
        original = {name: self.ccd(name) for name in ("alice", "bob")}
        PkiManager(self.config).ensure_client_ips(["bob", "alice", "carol"])
        for name, content in original.items():
            self.assertEqual(content, self.ccd(name))

    def test_prefix_change_rewrites_ipv6_and_preserves_ipv4_assignments(self):
        self.pki.ensure_client_ips(["alice"])
        saved = self.config.client_ips_path.read_bytes()
        self.config.vpn_ipv6_subnet = "fdab:1234:5678:9::/64"
        self.pki.ensure_client_ips(["alice"])
        self.assertIn("ifconfig-ipv6-push fdab:1234:5678:9:0:1:a08:2/64 fdab:1234:5678:9::1", self.ccd("alice"))
        self.assertNotIn("fd42:", self.ccd("alice"))
        self.assertEqual(saved, self.config.client_ips_path.read_bytes())

    def test_disabling_removes_ipv6_from_ccd(self):
        self.pki.ensure_client_ips(["alice"])
        self.config.experimental_ipv6 = False
        self.pki.ensure_client_ips(["alice"])
        self.assertEqual("ifconfig-push 10.8.0.2 255.255.255.0\n", self.ccd("alice"))

    def test_dynamic_and_shared_certificate_modes_use_pool_without_ccd(self):
        self.pki.ensure_client_ips(["alice"])
        for fixed, duplicate in ((False, False), (True, True)):
            with self.subTest(fixed=fixed, duplicate=duplicate):
                self.config.fixed_client_ips = fixed
                self.config.duplicate_cn = duplicate
                self.assertEqual({}, self.pki.ensure_client_ips(["alice"]))
                self.assertEqual([], list(self.config.ccd_dir.iterdir()))


if __name__ == "__main__":
    unittest.main()
