import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import openvpn_addon.config as config_module
from openvpn_addon import renderer
from openvpn_addon.config import load_config
from openvpn_addon.main import export_client
from openvpn_addon.validator import validate_config


class ClientFailoverTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.options = Path(self.tempdir.name) / "options.json"
        templates = Path(__file__).resolve().parent.parent / "addon" / "templates"
        for mock in (
            patch.object(config_module, "DATA_ROOT", Path(self.tempdir.name)),
            patch.object(renderer, "TEMPLATES_DIR", templates),
            patch.object(renderer, "_read_file", return_value="TEST DATA"),
            patch.object(renderer, "_read_cert_body", return_value="TEST CERTIFICATE"),
        ):
            mock.start()
            self.addCleanup(mock.stop)

    def load(self, **options):
        self.options.write_text(json.dumps({"public_endpoint": "vpn.example.com", **options}))
        return load_config(self.options)

    def enabled(self, **options):
        return self.load(**{
            "experimental_client_failover": True,
            "client_fallback_endpoints": ["backup.example.com"],
            **options,
        })

    def remotes(self, config):
        return [line for line in renderer.render_client_config(config, "demo").splitlines()
                if line.startswith("remote ")]

    def test_default_is_off_and_preserves_single_remote(self):
        config = self.load()
        self.assertIs(False, config.experimental_client_failover)
        self.assertEqual(["remote vpn.example.com 1194"], self.remotes(config))
        self.assertNotIn("server-poll-timeout", renderer.render_client_config(config, "demo"))

    def test_disabled_flag_ignores_dormant_settings(self):
        config = self.load(experimental_client_failover="false",
                           client_fallback_endpoints=["bad\nremote injected 443"],
                           client_connect_timeout=0)
        validate_config(config)
        self.assertEqual(["remote vpn.example.com 1194"], self.remotes(config))

    def test_orders_remotes_and_inherits_public_port_for_udp_and_tcp(self):
        for protocol in ("udp", "tcp"):
            with self.subTest(protocol=protocol):
                config = self.enabled(protocol=protocol, public_port=443,
                                      public_endpoint="vpn.example.com:1195",
                                      client_fallback_endpoints=["backup.example.com", "192.0.2.10:1196"])
                validate_config(config)
                self.assertEqual([
                    "remote vpn.example.com 1195", "remote backup.example.com 443",
                    "remote 192.0.2.10 1196",
                ], self.remotes(config))
                output = renderer.render_client_config(config, "demo")
                self.assertIn(f"proto {protocol}\n", output)
                self.assertIn("server-poll-timeout 30\n", output)
                self.assertNotIn("remote-random", output)

    def test_coerces_timeout_and_flag_from_options(self):
        config = self.enabled(experimental_client_failover="true", client_connect_timeout="15")
        self.assertEqual([], config.unknown_options)
        self.assertIn("server-poll-timeout 15\n", renderer.render_client_config(config, "demo"))

    def test_deduplicates_preserving_first_occurrence(self):
        config = self.enabled(client_fallback_endpoints=[
            "VPN.EXAMPLE.COM:1194", "backup.example.com:443", "BACKUP.example.com:443",
            "backup.example.com:1194",
        ])
        self.assertEqual(["remote vpn.example.com 1194", "remote backup.example.com 443",
                          "remote backup.example.com 1194"], self.remotes(config))

    def test_rejects_invalid_endpoints_in_validation_and_export(self):
        bad_values = ["", "bad host", "bad\nremote injected 443", "host\rverb 9", "host\t443",
                      "host:0", "host:65536", "host:abc", "host:１２３", "-host", "host_1",
                      "https://host", 'host"', "host#comment", "host;comment", "[::1]:443",
                      "2001:db8::1", "999.1.1.1", "a" * 64 + ".example", None, 123]
        for value in bad_values:
            with self.subTest(value=value):
                config = self.enabled(client_fallback_endpoints=[value])
                with self.assertRaises(ValueError):
                    validate_config(config)
                with self.assertRaises(ValueError):
                    renderer.render_client_config(config, "demo")

    def test_rejects_invalid_list_shape_or_size(self):
        for values in (None, "backup.example.com", {}, [], ["host"] * 9):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    validate_config(self.enabled(client_fallback_endpoints=values))

    def test_timeout_bounds(self):
        for timeout in (0, -1, 301):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                validate_config(self.enabled(client_connect_timeout=timeout))
        for timeout in (1, 300):
            validate_config(self.enabled(client_connect_timeout=timeout))

    def test_validates_primary_when_enabled(self):
        for endpoint in ("bad\nremote injected 443", "0.0.0.0", "::1"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                validate_config(self.enabled(public_endpoint=endpoint))

    def test_can_use_concrete_server_host_as_primary(self):
        config = self.enabled(public_endpoint="", server_host="192.0.2.1")
        validate_config(config)
        self.assertEqual("remote 192.0.2.1 1194", self.remotes(config)[0])

    def test_does_not_change_server_configuration(self):
        self.assertEqual(renderer.render_server_config(self.load()),
                         renderer.render_server_config(self.enabled()))

    def test_disabling_restores_original_profile(self):
        baseline = renderer.render_client_config(self.load(), "demo")
        config = self.enabled()
        self.assertNotEqual(baseline, renderer.render_client_config(config, "demo"))
        config.experimental_client_failover = False
        self.assertEqual(baseline, renderer.render_client_config(config, "demo"))

    def test_export_writes_alternate_remotes_to_private_profile(self):
        config = self.enabled()
        config.exports_dir.mkdir()
        result = export_client(config, "demo")
        output = result.export_path.read_text()
        self.assertIn("remote backup.example.com 1194\n", output)
        self.assertIn("server-poll-timeout 30\n", output)
        self.assertEqual(0o600, result.export_path.stat().st_mode & 0o777)
        self.assertIsNone(result.publish_path)

    def test_invalid_policy_fails_before_credentials_or_existing_export_are_touched(self):
        config = self.enabled(client_fallback_endpoints=["host\nverb 9"])
        config.exports_dir.mkdir()
        path = config.exports_dir / "demo.ovpn"
        path.write_text("existing profile")
        with self.assertRaises(ValueError):
            export_client(config, "demo")
        renderer._read_file.assert_not_called()
        renderer._read_cert_body.assert_not_called()
        self.assertEqual("existing profile", path.read_text())


if __name__ == "__main__":
    unittest.main()
