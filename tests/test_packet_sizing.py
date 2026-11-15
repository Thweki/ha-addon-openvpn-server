import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openvpn_addon import config as config_module, main, renderer
from openvpn_addon.config import AddonConfig, load_config
from openvpn_addon.validator import config_warnings, validate_config


class PacketSizingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        templates = Path(__file__).resolve().parents[1] / "addon" / "templates"
        for target, name, value in (
            (config_module, "DATA_ROOT", self.root),
            (renderer, "TEMPLATES_DIR", templates),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.config = AddonConfig(public_endpoint="vpn.example.com")
        # Deliberately fake fixtures: no real credentials are read or generated.
        for name, content in {
            "ca.crt": "TEST CA",
            "issued/demo.crt": "-----BEGIN CERTIFICATE-----\nTEST CERT\n-----END CERTIFICATE-----",
            "private/demo.key": "TEST KEY",
            "ta.key": "TEST TLS",
        }.items():
            path = self.config.pki_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def load(self, payload):
        path = self.root / "options.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return load_config(path)

    def assert_directives(self, expected):
        validate_config(self.config)
        for output in (
            renderer.render_server_config(self.config),
            renderer.render_client_config(self.config, "demo"),
        ):
            lines = output.splitlines()
            actual = [line for line in lines if line.split() and
                      line.split()[0] in {"fragment", "mssfix", "disable-dco"}]
            self.assertEqual(expected, actual)
            self.assertFalse(any(line.startswith(("tun-mtu ", "link-mtu ")) for line in lines))
            self.assertNotIn('push "fragment', output)
            self.assertNotIn('push "mssfix', output)

    def test_missing_options_default_off_for_udp_and_tcp(self):
        self.config = self.load({})
        self.assertIs(self.config.experimental_packet_sizing, False)
        self.assertEqual((0, 0), (self.config.fragment_size, self.config.mssfix_size))
        for protocol in ("udp", "tcp"):
            with self.subTest(protocol=protocol):
                self.config.protocol = protocol
                self.assert_directives([])

    def test_disabled_flag_ignores_stored_sizes_on_both_transports(self):
        self.config = self.load({
            "experimental_packet_sizing": "false", "fragment_size": "1300", "mssfix_size": "1200",
        })
        self.assertEqual([], self.config.unknown_options)
        for protocol in ("udp", "tcp"):
            with self.subTest(protocol=protocol):
                self.config.protocol = protocol
                self.assert_directives([])
                self.assertFalse(any("packet sizing" in item for item in config_warnings(self.config)))

    def test_enabled_without_sizes_has_no_effect(self):
        self.config.experimental_packet_sizing = True
        for protocol in ("udp", "tcp"):
            with self.subTest(protocol=protocol):
                self.config.protocol = protocol
                self.assert_directives([])

    def test_loaded_sizes_reach_both_shipped_templates(self):
        self.config = self.load({
            "experimental_packet_sizing": "true", "fragment_size": "1300", "mssfix_size": "1200",
        })
        self.assertEqual([], self.config.unknown_options)
        self.assert_directives(["disable-dco", "fragment 1300 mtu", "mssfix 1200 mtu"])

    def test_fragment_only_makes_mssfix_inherit_its_size(self):
        self.config.experimental_packet_sizing = True
        self.config.fragment_size = 1300
        self.assert_directives(["disable-dco", "fragment 1300 mtu", "mssfix"])

    def test_mssfix_can_be_used_without_fragmentation(self):
        self.config.experimental_packet_sizing = True
        self.config.mssfix_size = 1300
        self.assert_directives(["disable-dco", "mssfix 1300 mtu"])

    def test_size_boundaries(self):
        self.config.experimental_packet_sizing = True
        for size in (576, 1500):
            with self.subTest(size=size):
                self.config.fragment_size = self.config.mssfix_size = size
                self.assert_directives(["disable-dco", f"fragment {size} mtu", f"mssfix {size} mtu"])

    def test_invalid_sizes_rejected_even_with_flag_off(self):
        for option in ("fragment_size", "mssfix_size"):
            for size in (-1, 1, 575, 1501, 65536, True, 1300.5, "1300\npush anything"):
                for enabled in (False, True):
                    with self.subTest(option=option, size=size, enabled=enabled):
                        config = AddonConfig(experimental_packet_sizing=enabled, **{option: size})
                        with self.assertRaisesRegex(ValueError, option):
                            validate_config(config)

    def test_invalid_option_types_rejected_while_loading(self):
        for option in ("fragment_size", "mssfix_size"):
            for value in (True, False, 1300.5, 0.0, None, "1300 mtu", "1300.5", "1300\npush anything"):
                with self.subTest(option=option, value=value):
                    with self.assertRaises(ValueError):
                        self.load({option: value})

    def test_active_sizes_rejected_on_tcp(self):
        for fragment, mssfix in ((1300, 0), (0, 1200), (1300, 1200)):
            with self.subTest(fragment=fragment, mssfix=mssfix):
                config = AddonConfig(protocol="tcp", experimental_packet_sizing=True,
                                     fragment_size=fragment, mssfix_size=mssfix)
                with self.assertRaisesRegex(ValueError, "requires protocol: udp"):
                    validate_config(config)

    def test_warnings_explain_reimport_dco_and_fragment_compatibility(self):
        self.config.experimental_packet_sizing = True
        self.config.mssfix_size = 1200
        warnings = "\n".join(config_warnings(self.config))
        self.assertIn("DCO is disabled", warnings)
        self.assertIn("re-import all client profiles", warnings)
        self.assertNotIn("OpenVPN Connect", warnings)
        self.config.fragment_size = 1300
        self.assertIn("OpenVPN Connect", "\n".join(config_warnings(self.config)))

    def test_export_and_rollback_replace_packet_sizing_without_reissuing_certificates(self):
        self.config.experimental_packet_sizing = True
        self.config.fragment_size = 1300
        self.config.exports_dir.mkdir()
        with patch.object(main, "PkiManager", side_effect=AssertionError("PKI mutation")):
            result = main.export_client(self.config, "demo")
            self.assertIn("fragment 1300 mtu", result.export_path.read_text())
            self.config.experimental_packet_sizing = False
            result = main.export_client(self.config, "demo")
            output = result.export_path.read_text()
            self.assertNotIn("fragment", output)
            self.assertNotIn("mssfix", output)
            self.assertNotIn("disable-dco", output)
            self.assertIn("TEST CERT", output)
            self.assertIn("TEST KEY", output)


if __name__ == "__main__":
    unittest.main()
