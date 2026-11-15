import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openvpn_addon import config as config_module, main, renderer
from openvpn_addon.config import load_config
from openvpn_addon.pki import PkiManager
from openvpn_addon.validator import validate_config


class TlsAuthTests(unittest.TestCase):
    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.root = Path(tempdir.name)
        self.addCleanup(patch.stopall)
        patch.object(config_module, "DATA_ROOT", self.root).start()
        patch.object(
            renderer, "TEMPLATES_DIR",
            Path(__file__).resolve().parents[1] / "addon" / "templates",
        ).start()
        self.config = self.load(tls_crypt=False, experimental_tls_auth=True)
        for directory in ("issued", "private"):
            (self.config.pki_dir / directory).mkdir(parents=True)
        for name, content in {
            "ca.crt": "TEST CA",
            "issued/server.crt": "TEST SERVER CERTIFICATE",
            "issued/demo.crt": "-----BEGIN CERTIFICATE-----\nTEST CERT\n-----END CERTIFICATE-----",
            "private/demo.key": "TEST CLIENT KEY",
            "ta.key": "TEST STATIC KEY",
        }.items():
            (self.config.pki_dir / name).write_text(content, encoding="utf-8")

    def load(self, **options):
        path = self.root / "options.json"
        path.write_text(json.dumps(options), encoding="utf-8")
        return load_config(path)

    def test_flag_defaults_off_for_existing_options(self):
        config = self.load()
        self.assertFalse(config.experimental_tls_auth)
        self.assertTrue(config.tls_crypt)
        validate_config(config)
        server = renderer.render_server_config(config)
        client = renderer.render_client_config(config, "demo")
        self.assertIn(f"tls-crypt {config.tls_crypt_path}\n", server)
        self.assertIn("<tls-crypt>\nTEST STATIC KEY\n</tls-crypt>", client)
        self.assertNotIn("tls-auth", server + client)
        self.assertNotIn("key-direction", client)

    def test_enabled_option_survives_loading(self):
        self.assertTrue(self.config.experimental_tls_auth)
        self.assertEqual([], self.config.unknown_options)
        validate_config(self.config)

    def test_conflicting_modes_rejected_before_initialization(self):
        config = self.load(tls_crypt=True, experimental_tls_auth=True)
        with patch.object(main, "load_config", return_value=config), patch.object(main, "ensure_dirs") as dirs:
            with self.assertRaisesRegex(ValueError, "tls_crypt.*experimental_tls_auth"):
                main.cmd_init(argparse.Namespace())
            dirs.assert_not_called()

    def test_matching_server_and_inline_client_for_both_transports(self):
        for protocol in ("udp", "tcp"):
            with self.subTest(protocol=protocol):
                self.config.protocol = protocol
                server = renderer.render_server_config(self.config)
                client = renderer.render_client_config(self.config, "demo")
                self.assertIn(f"tls-auth {self.config.tls_crypt_path} 0\n", server)
                self.assertIn("key-direction 1\n<tls-auth>\nTEST STATIC KEY\n</tls-auth>", client)
                self.assertNotIn("tls-crypt", server + client)
                self.assertNotIn("TEST STATIC KEY", server)
                self.assertIn("auth SHA256\n", server)
                self.assertIn("auth SHA256\n", client)

    def test_both_modes_off_does_not_read_static_key(self):
        config = self.load(tls_crypt=False, experimental_tls_auth="false")
        config.tls_crypt_path.unlink()
        output = renderer.render_server_config(config) + renderer.render_client_config(config, "demo")
        self.assertNotIn("tls-auth", output)
        self.assertNotIn("tls-crypt", output)
        self.assertNotIn("key-direction", output)

    def test_missing_static_key_fails_export_instead_of_downgrading(self):
        self.config.tls_crypt_path.unlink()
        with self.assertRaises(FileNotFoundError):
            renderer.render_client_config(self.config, "demo")

    def test_startup_reuses_key_across_restarts_and_mode_changes(self):
        with patch.object(PkiManager, "_run"), patch("openvpn_addon.pki.subprocess.run") as run:
            for crypt, auth in ((False, True), (False, True), (True, False)):
                config = self.load(tls_crypt=crypt, experimental_tls_auth=auth)
                PkiManager(config).ensure_base()
            run.assert_not_called()
        self.assertEqual("TEST STATIC KEY", self.config.tls_crypt_path.read_text())

    def test_startup_generates_missing_shared_key(self):
        self.config.tls_crypt_path.unlink()
        with patch.object(PkiManager, "_run"), patch("openvpn_addon.pki.subprocess.run") as run:
            PkiManager(self.config).ensure_base()
            run.assert_called_once_with(
                ["openvpn", "--genkey", "secret", str(self.config.tls_crypt_path)], check=True,
            )

    def test_export_writes_matching_profile_with_private_permissions(self):
        self.config.public_endpoint = "vpn.example.com"
        self.config.exports_dir.mkdir()
        result = main.export_client(self.config, "demo")
        output = result.export_path.read_text()
        self.assertIn("<tls-auth>\nTEST STATIC KEY\n</tls-auth>", output)
        self.assertIn("key-direction 1\n", output)
        self.assertEqual(0o600, result.export_path.stat().st_mode & 0o777)
        self.assertIsNone(result.publish_path)


if __name__ == "__main__":
    unittest.main()
