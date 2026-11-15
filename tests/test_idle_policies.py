import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import openvpn_addon.config as config_module
from openvpn_addon import renderer
from openvpn_addon.config import AddonConfig, load_config
from openvpn_addon.pki import PkiManager
from openvpn_addon.validator import config_warnings, validate_config


class IdlePolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        root_patch = patch.object(config_module, "DATA_ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        template_patch = patch.object(
            renderer, "TEMPLATES_DIR", Path(__file__).resolve().parents[1] / "addon/templates"
        )
        template_patch.start()
        self.addCleanup(template_patch.stop)

    def load(self, **options):
        path = self.root / "options.json"
        path.write_text(json.dumps(options), encoding="utf-8")
        config = load_config(path)
        validate_config(config)
        return config

    def enabled(self, policies, **options):
        return self.load(
            experimental_client_idle=True, client_idle_policies=policies, **options
        )

    def test_default_is_off(self):
        config = self.load()
        self.assertIs(False, config.experimental_client_idle)
        self.assertEqual([], config.client_idle_policies)

    def test_disabled_feature_preserves_server_and_ccd_bytes(self):
        baseline = self.load(inactive_timeout=120)
        expected = renderer.render_server_config(baseline)
        PkiManager(baseline).ensure_client_ips(["laptop"])
        path = baseline.ccd_dir / "laptop"
        expected_ccd = path.read_bytes()
        disabled = self.load(
            inactive_timeout=120,
            experimental_client_idle=False,
            client_idle_policies=[{"client": "laptop", "timeout": 10}],
        )
        PkiManager(disabled).ensure_client_ips(["laptop"])
        self.assertEqual(expected, renderer.render_server_config(disabled))
        self.assertEqual(expected_ccd, path.read_bytes())
        self.assertEqual(config_warnings(baseline), config_warnings(disabled))

    def test_selected_clients_get_independent_policies_and_global_fallback(self):
        config = self.enabled([
            {"client": "laptop", "timeout": 900},
            {"client": "phone", "timeout": 600, "min_bytes": 1024},
            {"client": "bridge", "timeout": 0},
        ], inactive_timeout=1800)
        PkiManager(config).ensure_client_ips(["laptop", "phone", "bridge", "other"])
        for name, directive in [
            ("laptop", "inactive 900"),
            ("phone", "inactive 600 1024"),
            ("bridge", "inactive 0"),
        ]:
            with self.subTest(name=name):
                lines = (config.ccd_dir / name).read_text().splitlines()
                self.assertTrue(lines[0].startswith("ifconfig-push "))
                self.assertEqual([directive], lines[1:])
        self.assertNotIn("inactive", (config.ccd_dir / "other").read_text())
        server = renderer.render_server_config(config)
        self.assertIn("\ninactive 1800\n", server)
        self.assertNotIn("inactive 900", server)
        self.assertIn("client-config-dir", server)
        self.assertEqual([], config.unknown_options)

    def test_policy_removal_and_disabling_remove_old_directives(self):
        config = self.enabled([{"client": "laptop", "timeout": 60}])
        manager = PkiManager(config)
        manager.ensure_client_ips(["laptop"])
        path = config.ccd_dir / "laptop"
        self.assertIn("inactive 60", path.read_text())
        config.client_idle_policies = []
        manager.ensure_client_ips(["laptop"])
        self.assertNotIn("inactive", path.read_text())
        config.client_idle_policies = [{"client": "laptop", "timeout": 90}]
        manager.ensure_client_ips(["laptop"])
        self.assertIn("inactive 90", path.read_text())
        config.experimental_client_idle = False
        manager.ensure_client_ips(["laptop"])
        self.assertEqual("ifconfig-push 10.8.0.2 255.255.255.0\n", path.read_text())

    def test_policies_only_write_files_for_current_valid_clients(self):
        config = self.enabled([{"client": "future", "timeout": 60}])
        manager = PkiManager(config)
        manager.ensure_client_ips(["other"])
        self.assertFalse((config.ccd_dir / "future").exists())
        manager.ensure_client_ips(["other", "future"])
        self.assertIn("inactive 60", (config.ccd_dir / "future").read_text())
        manager.ensure_client_ips(["other"])
        self.assertFalse((config.ccd_dir / "future").exists())

    def test_enabled_policy_requires_fixed_addresses_and_unique_certificates(self):
        for options in [{"fixed_client_ips": False}, {"duplicate_cn": True}]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.enabled([], **options)
        self.load(experimental_client_idle=False, fixed_client_ips=False, duplicate_cn=True)

    def test_opt_in_must_be_a_real_boolean(self):
        for value in ["false", "true", "yes", "typo", 0, 1, None, [], {}]:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "boolean"):
                self.load(experimental_client_idle=value)

    def test_invalid_policies_are_rejected_without_echoing_values(self):
        invalid = [
            None, {}, "secret-value", [None], [{}],
            [{"client": "laptop"}],
            [{"client": "laptop", "timeout": 60, "unknown": "secret-value"}],
            [{"client": "laptop", "timeout": 60}] * 2,
        ]
        for name in ["../secret-value", "laptop\n", "DEFAULT", "server", "", 1]:
            invalid.append([{"client": name, "timeout": 60}])
        for field in ["timeout", "min_bytes"]:
            for value in [-1, 2147483648, True, 1.5, "secret-value", None]:
                policy = {"client": "laptop", "timeout": 60, field: value}
                invalid.append([policy])
        invalid.append([{"client": "laptop", "timeout": 0, "min_bytes": 1}])
        for policies in invalid:
            with self.subTest(policies=policies), self.assertRaises(ValueError) as error:
                self.enabled(policies)
            self.assertNotIn("secret-value", str(error.exception))

    def test_disabled_policy_payload_is_not_interpreted(self):
        config = self.load(experimental_client_idle=False, client_idle_policies="invalid")
        PkiManager(config).ensure_client_ips(["laptop"])
        self.assertNotIn("inactive", (config.ccd_dir / "laptop").read_text())

    def test_invalid_policy_cannot_partially_rewrite_ccd(self):
        config = self.enabled([{"client": "laptop", "timeout": 60}])
        manager = PkiManager(config)
        manager.ensure_client_ips(["laptop"])
        path = config.ccd_dir / "laptop"
        original = path.read_bytes()
        config.client_idle_policies.append({"client": "other", "timeout": "bad"})
        with self.assertRaises(ValueError):
            manager.ensure_client_ips(["laptop", "other"])
        self.assertEqual(original, path.read_bytes())
        self.assertFalse((config.ccd_dir / "other").exists())

    def test_enabled_feature_is_visible_in_existing_warnings(self):
        config = self.enabled([])
        self.assertTrue(any("EXPERIMENTAL" in item for item in config_warnings(config)))


if __name__ == "__main__":
    unittest.main()
