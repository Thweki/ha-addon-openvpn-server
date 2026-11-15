"""Validate exported credentials with real OpenSSL, without starting a VPN."""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openvpn_addon import config as config_module, renderer
from openvpn_addon.config import AddonConfig
from openvpn_addon.main import export_client


class ProfileCryptoTests(unittest.TestCase):
    def setUp(self):
        if shutil.which("openssl") is None:
            self.fail("OpenSSL is required for the profile integration tests")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        templates = Path(__file__).resolve().parents[2] / "addon" / "templates"
        for target, name, value in (
            (config_module, "DATA_ROOT", self.root),
            (config_module, "SHARE_ROOT", self.root / "share"),
            (renderer, "TEMPLATES_DIR", templates),
        ):
            override = patch.object(target, name, value)
            override.start()
            self.addCleanup(override.stop)

        self.config = AddonConfig(
            public_endpoint="vpn.example.test",
            tls_crypt=False,
            publish_profiles_to_share=True,
        )
        for path in (
            self.config.pki_dir / "issued",
            self.config.pki_dir / "private",
            self.config.exports_dir,
            self.config.share_exports_dir,
        ):
            path.mkdir(parents=True)
        self.ca_key = self.root / "ca.key"
        self.ca_cert = self.config.pki_dir / "ca.crt"
        self.openssl(
            "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=Profile integration CA",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-keyout", self.ca_key, "-out", self.ca_cert,
        )

    def openssl(self, *args):
        result = subprocess.run(
            ["openssl", *map(str, args)], capture_output=True, timeout=30,
        )
        # Never include captured PEM/profile data in a test failure or CI log.
        self.assertEqual(result.returncode, 0, f"openssl {args[0]} failed")
        return result.stdout

    def check_export(self, *key_options):
        key = self.config.pki_dir / "private" / "demo.key"
        cert = self.config.pki_dir / "issued" / "demo.crt"
        csr = self.root / "client.csr"
        self.openssl(
            "req", "-new", *key_options, "-nodes",
            "-subj", "/CN=demo", "-keyout", key, "-out", csr,
        )
        extensions = self.root / "client.ext"
        extensions.write_text(
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature\n"
            "extendedKeyUsage=clientAuth\n", encoding="utf-8",
        )
        self.openssl(
            "x509", "-req", "-in", csr, "-CA", self.ca_cert,
            "-CAkey", self.ca_key, "-set_serial", "2", "-days", "1",
            "-extfile", extensions, "-out", cert,
        )
        # EasyRSA certificates can have human-readable text before the PEM block.
        cert.write_bytes(self.openssl("x509", "-in", cert, "-text"))

        exported = export_client(self.config, "demo")
        self.assertIsNotNone(exported.publish_path)
        for profile_path in (exported.export_path, exported.publish_path):
            with self.subTest(destination=profile_path.parent.name):
                profile = profile_path.read_text(encoding="utf-8")
                blocks = {}
                for tag in ("ca", "cert", "key"):
                    matches = re.findall(rf"(?ms)^<{tag}>\n(.*?)^</{tag}>$", profile)
                    self.assertEqual(len(matches), 1, f"expected one inline {tag} block")
                    blocks[tag] = self.root / f"inline-{tag}.pem"
                    blocks[tag].write_text(matches[0], encoding="utf-8")

                self.openssl(
                    "verify", "-purpose", "sslclient", "-CAfile", blocks["ca"], blocks["cert"],
                )
                self.openssl("pkey", "-in", blocks["key"], "-check", "-noout")
                certificate_key = self.openssl("x509", "-in", blocks["cert"], "-pubkey", "-noout")
                private_key = self.openssl("pkey", "-in", blocks["key"], "-pubout")
                self.assertTrue(certificate_key == private_key, "certificate and private key must match")
                actual = self.openssl("x509", "-in", blocks["cert"], "-outform", "DER")
                expected = self.openssl("x509", "-in", cert, "-outform", "DER")
                self.assertTrue(actual == expected, "export must contain the requested client certificate")

    def test_rsa_profile_credentials(self):
        self.check_export("-newkey", "rsa:2048")

    def test_ec_profile_credentials(self):
        self.check_export("-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1")


if __name__ == "__main__":
    unittest.main()
