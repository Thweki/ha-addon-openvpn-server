import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import openvpn_addon.config as config_module
from openvpn_addon import renderer
from openvpn_addon.config import AddonConfig, load_config
from openvpn_addon.main import export_client


class ServerPinningTests(unittest.TestCase):
    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.root = Path(tempdir.name)
        root_patch = patch.object(config_module, "DATA_ROOT", self.root)
        template_patch = patch.object(
            renderer, "TEMPLATES_DIR", Path(__file__).resolve().parents[1] / "addon/templates"
        )
        root_patch.start()
        template_patch.start()
        self.addCleanup(root_patch.stop)
        self.addCleanup(template_patch.stop)
        self.config = AddonConfig(public_endpoint="vpn.example.com")
        for directory in ("issued", "private"):
            (self.config.pki_dir / directory).mkdir(parents=True)
        (self.config.pki_dir / "ca.crt").write_text("CA", encoding="utf-8")
        (self.config.pki_dir / "issued/demo.crt").write_text(
            "-----BEGIN CERTIFICATE-----\nCLIENT\n-----END CERTIFICATE-----\n", encoding="utf-8"
        )
        (self.config.pki_dir / "private/demo.key").write_text("TEST KEY", encoding="utf-8")
        (self.config.pki_dir / "ta.key").write_text("TEST TLS KEY", encoding="utf-8")
        self.server_cert = self.config.pki_dir / "issued/server.crt"

    def test_flag_defaults_off_in_config_and_addon_options(self):
        self.assertFalse(self.config.experimental_server_pinning)
        manifest = (Path(__file__).resolve().parents[1] / "addon/config.yaml").read_text()
        self.assertIn("  experimental_server_pinning: false\n", manifest)
        self.assertIn("  experimental_server_pinning: bool\n", manifest)

    def test_flag_loads_from_options(self):
        options = self.root / "options.json"
        for value, expected in ((True, True), ("true", True), (False, False), ("false", False)):
            with self.subTest(value=value):
                options.write_text(json.dumps({"experimental_server_pinning": value}))
                config = load_config(options)
                self.assertIs(config.experimental_server_pinning, expected)
                self.assertEqual([], config.unknown_options)

    def test_disabled_needs_no_server_certificate_or_openssl(self):
        with patch.object(renderer, "subprocess") as process:
            output = renderer.render_client_config(self.config, "demo")
        process.run.assert_not_called()
        self.assertNotIn("peer-fingerprint", output)
        self.assertIn("remote-cert-tls server", output)

    def test_enabled_pins_server_der_and_keeps_existing_profile_options(self):
        self.config.experimental_server_pinning = True
        der = b"test server DER bytes"
        expected = hashlib.sha256(der).digest().hex(":").upper()
        with patch.object(renderer.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, der)) as run:
            for protocol in ("udp", "tcp"):
                for tls_crypt in (True, False):
                    with self.subTest(protocol=protocol, tls_crypt=tls_crypt):
                        self.config.protocol = protocol
                        self.config.tls_crypt = tls_crypt
                        output = renderer.render_client_config(self.config, "demo")
                        self.assertEqual(1, output.count("peer-fingerprint "))
                        self.assertIn(f"peer-fingerprint {expected}\n", output)
                        self.assertIn("remote-cert-tls server\n", output)
                        self.assertIn("<ca>\nCA\n</ca>", output)
                        self.assertIn(f"proto {protocol}\n", output)
                        self.assertEqual(tls_crypt, "<tls-crypt>" in output)
        self.assertEqual(
            ["openssl", "x509", "-in", str(self.server_cert), "-outform", "DER"],
            run.call_args.args[0],
        )

    def test_export_fails_closed_without_overwriting_existing_profile(self):
        self.config.experimental_server_pinning = True
        self.config.exports_dir.mkdir()
        target = self.config.exports_dir / "demo.ovpn"
        target.write_text("existing profile", encoding="utf-8")
        for error in (
            FileNotFoundError(),
            subprocess.CalledProcessError(1, "openssl", stderr=b"certificate parse failure"),
            subprocess.TimeoutExpired("openssl", 10),
        ):
            with self.subTest(error=type(error).__name__):
                with patch.object(renderer.subprocess, "run", side_effect=error):
                    with self.assertRaisesRegex(ValueError, "server certificate"):
                        export_client(self.config, "demo")
                self.assertEqual("existing profile", target.read_text())

    def test_empty_certificate_output_is_rejected(self):
        self.config.experimental_server_pinning = True
        with patch.object(renderer.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"")):
            with self.assertRaisesRegex(ValueError, "server certificate"):
                renderer.render_client_config(self.config, "demo")

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL is unavailable")
    def test_real_certificate_fingerprints_and_rotation(self):
        self.config.experimental_server_pinning = True
        self.config.exports_dir.mkdir()
        fingerprints = []
        for algorithm in ("rsa:2048", "ec"):
            with self.subTest(algorithm=algorithm):
                command = [
                    "openssl", "req", "-x509", "-newkey", algorithm, "-nodes",
                    "-keyout", str(self.root / "test-server.key"),
                    "-out", str(self.server_cert), "-subj", "/CN=server", "-days", "1",
                ]
                if algorithm == "ec":
                    command.extend(["-pkeyopt", "ec_paramgen_curve:P-256"])
                subprocess.run(command, check=True, capture_output=True)
                expected = subprocess.run(
                    ["openssl", "x509", "-in", str(self.server_cert), "-noout", "-fingerprint", "-sha256"],
                    check=True, capture_output=True, text=True,
                ).stdout.strip().split("=", 1)[1]
                result = export_client(self.config, "demo")
                self.assertIn(f"peer-fingerprint {expected}\n", result.export_path.read_text())
                fingerprints.append(expected)
        self.assertNotEqual(*fingerprints)

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL is unavailable")
    def test_missing_or_invalid_server_certificate_is_rejected(self):
        self.config.experimental_server_pinning = True
        with self.assertRaisesRegex(ValueError, "server certificate"):
            renderer.render_client_config(self.config, "demo")
        self.server_cert.write_text("not a certificate", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "server certificate"):
            renderer.render_client_config(self.config, "demo")


if __name__ == "__main__":
    unittest.main()
