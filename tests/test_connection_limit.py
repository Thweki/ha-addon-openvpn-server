import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openvpn_addon import renderer
from openvpn_addon.config import AddonConfig, load_config
from openvpn_addon.validator import validate_config


REPO = Path(__file__).resolve().parent.parent


class ConnectionLimitTests(unittest.TestCase):
    def load(self, payload):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "options.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            config = load_config(path)
        validate_config(config)
        return config

    def render(self, config):
        with patch.object(renderer, "TEMPLATES_DIR", REPO / "addon/templates"):
            return renderer.render_server_config(config)

    def test_old_options_and_explicit_zero_leave_limit_disabled(self):
        for payload in ({}, {"max_clients": 0}):
            with self.subTest(payload=payload):
                config = self.load(payload)
                self.assertEqual(0, config.max_clients)
                self.assertNotIn("max-clients", self.render(config))
                self.assertIn("keepalive 10 120", self.render(config))

    def test_options_reach_real_template_for_both_transports_and_identity_modes(self):
        for protocol in ("udp", "tcp"):
            for duplicate_cn in (False, True):
                with self.subTest(protocol=protocol, duplicate_cn=duplicate_cn):
                    config = self.load({
                        "max_clients": 8,
                        "protocol": protocol,
                        "duplicate_cn": duplicate_cn,
                    })
                    self.assertEqual([], config.unknown_options)
                    lines = self.render(config).splitlines()
                    self.assertEqual(["max-clients 8"], [line for line in lines if "max-clients" in line])
                    self.assertIn(f"proto {protocol}", lines)

    def test_integer_strings_and_boundaries(self):
        for value in (1, 1024, "8"):
            with self.subTest(value=value):
                config = self.load({"max_clients": value})
                self.assertEqual(int(value), config.max_clients)
                self.assertIn(f"max-clients {int(value)}", self.render(config).splitlines())

    def test_invalid_options_are_rejected_instead_of_coerced_to_a_different_limit(self):
        for value in (-1, 1025, True, False, 1.5, 0.5, None, [], "many", "1.5", "8\npush reset"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.load({"max_clients": value})

    def test_direct_configuration_is_validated(self):
        for value in (-1, 1025, True, 1.5, "8", None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "max_clients"):
                    validate_config(AddonConfig(max_clients=value))

    def test_setting_zero_removes_a_previously_enabled_limit(self):
        config = self.load({"max_clients": 8})
        self.assertIn("max-clients 8", self.render(config))
        config.max_clients = 0
        self.assertNotIn("max-clients", self.render(config))

    def test_supervisor_default_schema_and_translations(self):
        manifest = (REPO / "addon/config.yaml").read_text(encoding="utf-8")
        options, schema = manifest.split("\nschema:\n", 1)
        self.assertIn("  max_clients: 0\n", options)
        self.assertIn("  max_clients: int(0,1024)\n", schema)
        for language in ("en", "zh-Hans"):
            translation = json.loads((REPO / f"addon/translations/{language}.json").read_text(encoding="utf-8"))
            self.assertTrue(translation["configuration"]["max_clients"]["name"])
            self.assertTrue(translation["configuration"]["max_clients"]["description"])


if __name__ == "__main__":
    unittest.main()
