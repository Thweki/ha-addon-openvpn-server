import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openvpn_addon import renderer
from openvpn_addon.config import AddonConfig, load_config
from openvpn_addon.validator import validate_config


class ClientVerificationTests(unittest.TestCase):
    def setUp(self):
        templates = Path(__file__).resolve().parent.parent / "addon" / "templates"
        self.template_patch = patch.object(renderer, "TEMPLATES_DIR", templates)
        self.template_patch.start()
        self.addCleanup(self.template_patch.stop)

    def load(self, payload):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "options.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return load_config(path)

    def enabled(self, **options):
        return self.load({
            "experimental_client_verify": True,
            "client_verify_mode": "exact",
            "client_verify_name": "home-laptop",
            **options,
        })

    def test_defaults_leave_client_verification_disabled(self):
        for config in (AddonConfig(), self.load({})):
            self.assertIs(config.experimental_client_verify, False)
            self.assertEqual(config.client_verify_mode, "exact")
            self.assertEqual(config.client_verify_name, "")
            validate_config(config)
            self.assertNotIn("verify-x509-name", renderer.render_server_config(config))

    def test_loads_options_without_unknown_option_warnings(self):
        config = self.enabled()
        self.assertEqual(config.unknown_options, [])
        self.assertIs(config.experimental_client_verify, True)
        self.assertEqual(config.client_verify_name, "home-laptop")

    def test_disabled_flag_ignores_stale_policy(self):
        config = self.enabled(
            experimental_client_verify="false",
            client_verify_mode="obsolete",
            client_verify_name="bad\nconfiguration",
        )
        validate_config(config)
        output = renderer.render_server_config(config)
        self.assertNotIn("verify-x509-name", output)
        self.assertNotIn("bad", output)

    def test_exact_cn_policy_is_enforced_by_server_template(self):
        config = self.enabled()
        validate_config(config)
        output = renderer.render_server_config(config)
        self.assertIn("verify-x509-name home-laptop name", output.splitlines())
        self.assertIn("remote-cert-tls client", output.splitlines())
        self.assertIn(f"ca {config.pki_dir / 'ca.crt'}", output.splitlines())
        self.assertIn(f"crl-verify {config.crl_path}", output.splitlines())
        self.assertNotIn("tls-verify", output)

    def test_prefix_policy_supports_distinct_device_certificates(self):
        config = self.enabled(client_verify_mode="prefix", client_verify_name="home-")
        validate_config(config)
        output = renderer.render_server_config(config)
        self.assertIn("verify-x509-name home- name-prefix", output.splitlines())
        self.assertIn(f"client-config-dir {config.ccd_dir}", output.splitlines())

    def test_policy_also_applies_with_shared_certificates(self):
        config = self.enabled(duplicate_cn=True)
        validate_config(config)
        output = renderer.render_server_config(config)
        self.assertIn("verify-x509-name home-laptop name", output.splitlines())
        self.assertIn("duplicate-cn", output.splitlines())

    def test_invalid_modes_fail_before_rendering(self):
        for mode in ("", "subject", "san", "name", "EXACT", "exact\nclient", None, [], 1):
            with self.subTest(mode=mode):
                config = self.enabled(client_verify_mode=mode)
                with self.assertRaisesRegex(ValueError, "client_verify_mode"):
                    validate_config(config)
                with self.assertRaisesRegex(ValueError, "client_verify_mode"):
                    renderer.render_server_config(config)

    def test_empty_malformed_and_injected_names_fail_closed(self):
        for name in ("", " ", "home-*", "home laptop", "home\n", "home\r",
                     "home\nverify-client-cert none", 'home"', "home\\", "home;",
                     "../home", "équipe", "a" * 64, None, [], 1):
            for mode in ("exact", "prefix"):
                with self.subTest(name=name, mode=mode):
                    config = self.enabled(client_verify_name=name, client_verify_mode=mode)
                    with self.assertRaisesRegex(ValueError, "client_verify_name"):
                        validate_config(config)
                    with self.assertRaisesRegex(ValueError, "client_verify_name"):
                        renderer.render_server_config(config)

    def test_supported_names_are_preserved_literally(self):
        for name in ("A", "Home_laptop-1.example", "a" * 63):
            with self.subTest(name=name):
                config = self.enabled(client_verify_name=name)
                validate_config(config)
                self.assertIn(
                    f"verify-x509-name {name} name",
                    renderer.render_server_config(config).splitlines(),
                )

    def test_disabling_policy_removes_the_restriction(self):
        config = self.enabled()
        self.assertIn("verify-x509-name", renderer.render_server_config(config))
        config.experimental_client_verify = False
        self.assertNotIn("verify-x509-name", renderer.render_server_config(config))

    def test_client_profile_does_not_inherit_server_admission_policy(self):
        config = self.enabled(public_endpoint="vpn.example.com")
        with patch.object(renderer, "_read_file", return_value="fixture"), \
                patch.object(renderer, "_read_cert_body", return_value="fixture"):
            enabled_output = renderer.render_client_config(config, "home-laptop")
            config.experimental_client_verify = False
            disabled_output = renderer.render_client_config(config, "home-laptop")
        self.assertEqual(enabled_output, disabled_output)


if __name__ == "__main__":
    unittest.main()
