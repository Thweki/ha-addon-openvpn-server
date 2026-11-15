import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openvpn_addon import renderer
from openvpn_addon.config import load_config
from openvpn_addon.validator import config_warnings, validate_config


class ConnectionRateLimitTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.options_path = Path(self.tempdir.name) / "options.json"
        templates = Path(__file__).resolve().parent.parent / "addon" / "templates"
        self.templates_patch = patch.object(renderer, "TEMPLATES_DIR", templates)
        self.templates_patch.start()
        self.addCleanup(self.templates_patch.stop)

    def load(self, **options):
        self.options_path.write_text(json.dumps(options), encoding="utf-8")
        config = load_config(self.options_path)
        validate_config(config)
        return config

    def test_defaults_leave_rate_limit_disabled(self):
        config = self.load()
        self.assertIs(False, config.experimental_connection_rate_limit)
        self.assertEqual(10, config.connection_rate_limit)
        self.assertEqual(60, config.connection_rate_window)
        self.assertNotIn("connect-freq", renderer.render_server_config(config))

    def test_values_alone_do_not_enable_rate_limit(self):
        config = self.load(connection_rate_limit=2, connection_rate_window=30)
        self.assertNotIn("connect-freq", renderer.render_server_config(config))

    def test_enabled_limit_is_rendered_once_for_udp_and_tcp(self):
        for protocol in ("udp", "tcp"):
            with self.subTest(protocol=protocol):
                config = self.load(
                    protocol=protocol,
                    experimental_connection_rate_limit=True,
                    connection_rate_limit=7,
                    connection_rate_window=45,
                )
                self.assertEqual([], config.unknown_options)
                lines = renderer.render_server_config(config).splitlines()
                self.assertEqual(["connect-freq 7 45"], [line for line in lines if "connect-freq" in line])

    def test_string_options_are_coerced(self):
        config = self.load(
            experimental_connection_rate_limit="true",
            connection_rate_limit="12",
            connection_rate_window="90",
        )
        self.assertIn("\nconnect-freq 12 90\n", renderer.render_server_config(config))

    def test_disabling_removes_directive(self):
        config = self.load(experimental_connection_rate_limit=True)
        self.assertIn("\nconnect-freq 10 60\n", renderer.render_server_config(config))
        config = self.load(experimental_connection_rate_limit="false")
        self.assertNotIn("connect-freq", renderer.render_server_config(config))

    def test_invalid_ranges_are_rejected_even_when_disabled(self):
        for enabled in (False, True):
            for option, maximum in (("connection_rate_limit", 1000), ("connection_rate_window", 3600)):
                for value in (-1, 0, maximum + 1):
                    with self.subTest(enabled=enabled, option=option, value=value):
                        with self.assertRaisesRegex(ValueError, option):
                            self.load(experimental_connection_rate_limit=enabled, **{option: value})

    def test_range_boundaries_are_accepted(self):
        for count, window in ((1, 1), (1000, 3600)):
            with self.subTest(count=count, window=window):
                config = self.load(
                    experimental_connection_rate_limit=True,
                    connection_rate_limit=count,
                    connection_rate_window=window,
                )
                self.assertIn(f"\nconnect-freq {count} {window}\n", renderer.render_server_config(config))

    def test_non_numeric_options_are_rejected(self):
        for option in ("connection_rate_limit", "connection_rate_window"):
            with self.subTest(option=option):
                with self.assertRaises(ValueError):
                    self.load(**{option: "10\npush reset"})

    def test_warns_about_shared_budget_only_when_enabled(self):
        config = self.load(experimental_connection_rate_limit=True)
        warnings = config_warnings(config)
        self.assertTrue(any("connection rate limit" in warning and "legitimate" in warning for warning in warnings))
        config.experimental_connection_rate_limit = False
        self.assertFalse(any("connection rate limit" in warning for warning in config_warnings(config)))


if __name__ == "__main__":
    unittest.main()
