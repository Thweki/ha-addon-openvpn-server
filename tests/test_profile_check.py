import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import openvpn_addon.config as config_module
import openvpn_addon.renderer as renderer
from openvpn_addon.config import AddonConfig
from openvpn_addon.main import build_parser
from openvpn_addon.profile_check import MAX_PROFILE_BYTES, check_profile


class ProfileCheckCommandTests(unittest.TestCase):
    def invoke(self, *arguments):
        args = build_parser().parse_args(["client", "check-profile", "/not/read.ovpn", *arguments])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = args.func(args)
        return code, json.loads(output.getvalue())

    def test_disabled_by_default_before_reading_config_or_profile(self):
        with patch("openvpn_addon.main.load_and_validate") as load, \
                patch("openvpn_addon.profile_check._read_bounded") as read:
            code, report = self.invoke()
        self.assertEqual(2, code)
        self.assertEqual("disabled", report["checks"][0]["check"])
        load.assert_not_called()
        read.assert_not_called()

    def test_configuration_error_does_not_expose_option_values(self):
        with patch("openvpn_addon.main.load_and_validate", side_effect=ValueError("secret-value")):
            code, report = self.invoke("--experimental-profile-check")
        self.assertEqual(2, code)
        self.assertNotIn("secret-value", json.dumps(report))

    def test_enabled_command_returns_checker_status(self):
        with patch("openvpn_addon.main.load_and_validate", return_value=AddonConfig()), \
                patch("openvpn_addon.profile_check.check_profile", return_value=({"status": "fail"}, 1)) as check:
            code, report = self.invoke("--experimental-profile-check")
        self.assertEqual((1, {"status": "fail"}), (code, report))
        self.assertEqual(Path("/not/read.ovpn"), check.call_args.args[1])


@unittest.skipUnless(shutil.which("openssl"), "OpenSSL unavailable")
class ProfileCheckTests(unittest.TestCase):
    @classmethod
    def openssl(cls, root, *args):
        result = subprocess.run(["openssl", *args], cwd=root, capture_output=True, timeout=15)
        if result.returncode:
            # Do not expose command output or private fixture material on a test failure.
            raise AssertionError("OpenSSL fixture operation failed")

    @classmethod
    def setUpClass(cls):
        cls.fixtures = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.fixtures.cleanup)
        root = Path(cls.fixtures.name)
        (root / "pki" / "private").mkdir(parents=True)
        (root / "pki" / "issued").mkdir()
        (root / "pki" / "index.txt").touch()
        (root / "pki" / "serial").write_text("1000\n")
        (root / "ca.cnf").write_text("""[ca]
default_ca = local
[local]
database = pki/index.txt
serial = pki/serial
new_certs_dir = pki/issued
certificate = pki/ca.crt
private_key = pki/private/ca.key
default_md = sha256
default_days = 30
default_crl_days = 30
policy = names
[names]
commonName = supplied
[client]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature
extendedKeyUsage = clientAuth
[server]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature
extendedKeyUsage = serverAuth
""")
        cls.openssl(root, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                    "-nodes", "-keyout", "pki/private/ca.key", "-out", "pki/ca.crt", "-days", "30",
                    "-subj", "/CN=Preflight Test CA", "-addext", "basicConstraints=critical,CA:TRUE",
                    "-addext", "keyUsage=critical,keyCertSign,cRLSign")
        for name in ("rsa", "ec", "expired", "server"):
            key_args = ["rsa:2048"] if name == "rsa" else ["ec", "-pkeyopt", "ec_paramgen_curve:P-256"]
            cls.openssl(root, "req", "-new", "-newkey", *key_args, "-nodes", "-subj", f"/CN={name}",
                        "-keyout", f"pki/private/{name}.key", "-out", f"{name}.csr")
            dates = ["-startdate", "20200101000000Z", "-enddate", "20200102000000Z"] if name == "expired" else []
            cls.openssl(root, "ca", "-batch", "-config", "ca.cnf", "-extensions",
                        "server" if name == "server" else "client", "-in", f"{name}.csr",
                        "-out", f"pki/issued/{name}.crt", *dates)
        cls.openssl(root, "ca", "-batch", "-config", "ca.cnf", "-gencrl", "-out", "pki/crl.pem")
        cls.openssl(root, "req", "-x509", "-key", "pki/private/ec.key", "-subj", "/CN=Other CA",
                    "-out", "other-ca.crt", "-days", "30")
        (root / "pki" / "ta.key").write_text(
            "# Test static key\n-----BEGIN OpenVPN Static key V1-----\n" + "a1" * 256
            + "\n-----END OpenVPN Static key V1-----\n")

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        shutil.copytree(self.fixtures.name, self.root, dirs_exist_ok=True)
        root_patch = patch.object(config_module, "DATA_ROOT", self.root)
        templates_patch = patch.object(renderer, "TEMPLATES_DIR", Path(__file__).resolve().parents[1] / "addon/templates")
        root_patch.start()
        templates_patch.start()
        self.addCleanup(root_patch.stop)
        self.addCleanup(templates_patch.stop)
        self.config = AddonConfig(public_endpoint="vpn.example.test")
        self.path = self.root / "client.ovpn"
        self.profile = renderer.render_client_config(self.config, "rsa")

    def check(self, profile=None):
        self.path.write_text(self.profile if profile is None else profile)
        with contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = check_profile(self.config, self.path)
        self.assertEqual("", stdout.getvalue() + stderr.getvalue())
        serialized = json.dumps(result)
        self.assertNotIn("BEGIN", serialized)
        self.assertNotIn("vpn.example.test", serialized)
        self.assertNotIn(str(self.root), serialized)
        return result

    def assert_failed(self, result, check):
        report, code = result
        self.assertEqual(1, code)
        self.assertEqual("fail", report["status"])
        self.assertEqual("fail", next(item["status"] for item in report["checks"] if item["check"] == check))

    def test_current_rsa_and_ec_exports_pass_without_writing_state(self):
        for name in ("rsa", "ec"):
            with self.subTest(name=name):
                profile = renderer.render_client_config(self.config, name)
                before = {p.relative_to(self.root): (p.read_bytes(), p.stat().st_mtime_ns)
                          for p in self.root.rglob("*") if p.is_file() and p != self.path}
                report, code = self.check(profile)
                self.assertEqual(("pass", 0), (report["status"], code))
                after = {p.relative_to(self.root): (p.read_bytes(), p.stat().st_mtime_ns)
                         for p in self.root.rglob("*") if p.is_file() and p != self.path}
                self.assertTrue(before == after, "Preflight changed fixture files")

    def test_stale_connection_settings_fail(self):
        for old, new in (("vpn.example.test", "old.example.test"), ("1194", "443"),
                         ("proto udp", "proto tcp"), ("remote-cert-tls server", "remote-cert-tls client"),
                         ("auth SHA256", "auth SHA512"), ("tls-version-min 1.2", "tls-version-min 1.0")):
            with self.subTest(setting=old):
                self.assert_failed(self.check(self.profile.replace(old, new)), "connection_settings")

    def test_endpoint_port_override_and_tcp_match_renderer(self):
        self.config.public_endpoint = "vpn.example.test:443"
        self.config.protocol = "tcp"
        report, code = self.check(renderer.render_client_config(self.config, "ec"))
        self.assertEqual(("pass", 0), (report["status"], code))

    def test_mismatched_key_fails(self):
        wrong = (self.root / "pki/private/ec.key").read_text().strip()
        original = (self.root / "pki/private/rsa.key").read_text().strip()
        self.assert_failed(self.check(self.profile.replace(original, wrong)), "key_match")

    def test_other_ca_fails(self):
        original = (self.root / "pki/ca.crt").read_text().strip()
        wrong = (self.root / "other-ca.crt").read_text().strip()
        self.assert_failed(self.check(self.profile.replace(original, wrong)), "ca_match")

    def test_embedded_ca_cannot_supply_its_own_trust(self):
        other = (self.root / "other-ca.crt").read_text().strip()
        profile = renderer.render_client_config(self.config, "ec")
        start = profile.index("<cert>") + len("<cert>")
        end = profile.index("</cert>")
        profile = profile[:start] + "\n" + other + "\n" + profile[end:]
        profile = profile.replace((self.root / "pki/ca.crt").read_text().strip(), other)
        result = self.check(profile)
        self.assert_failed(result, "ca_match")
        self.assert_failed(result, "certificate")

    def test_server_side_private_keys_are_not_required(self):
        shutil.rmtree(self.root / "pki/private")
        self.assertEqual(0, self.check()[1])

    def test_expired_and_server_certificates_fail(self):
        for name in ("expired", "server"):
            with self.subTest(name=name):
                self.assert_failed(self.check(renderer.render_client_config(self.config, name)), "certificate")

    def test_revoked_profile_fails_even_if_export_still_exists(self):
        self.openssl(self.root, "ca", "-batch", "-config", "ca.cnf", "-revoke", "pki/issued/rsa.crt")
        self.openssl(self.root, "ca", "-batch", "-config", "ca.cnf", "-gencrl", "-out", "pki/crl.pem")
        self.assert_failed(self.check(), "revocation")

    def test_missing_corrupt_and_expired_crls_fail(self):
        self.config.crl_path.unlink()
        self.assert_failed(self.check(), "revocation")
        self.config.crl_path.write_text("invalid")
        self.assert_failed(self.check(), "revocation")
        self.openssl(self.root, "ca", "-batch", "-config", "ca.cnf", "-gencrl", "-out", "pki/crl.pem",
                     "-crl_lastupdate", "20200101000000Z", "-crl_nextupdate", "20200102000000Z")
        self.assert_failed(self.check(), "revocation")

    def test_tls_key_mismatch_and_missing_block_fail(self):
        self.assert_failed(self.check(self.profile.replace("a1" * 256, "b2" * 256)), "tls_crypt")
        without_tls = self.profile[:self.profile.index("<tls-crypt>")]
        self.assert_failed(self.check(without_tls), "tls_crypt")
        self.config.tls_crypt = False
        self.assertEqual(0, self.check(without_tls)[1])
        self.assert_failed(self.check(), "tls_crypt")

    def test_rejects_unsupported_profiles_before_openssl(self):
        ca_block = self.profile[self.profile.index("<ca>"):self.profile.index("</ca>") + 5]
        invalid = [self.profile + "\nremote other.example 443\n", self.profile + ca_block,
                   self.profile + "\nconfig /not/read\n", self.profile + "\nup /not/executed\n",
                   self.profile.replace("</cert>", "</key>"), self.profile.replace("<cert>", "<unknown>"),
                   self.profile.replace("client\n", ""), self.profile.replace("<key>", "key /not/read\n"),
                   self.profile.replace("BEGIN PRIVATE KEY", "BEGIN ENCRYPTED PRIVATE KEY")]
        for profile in invalid:
            with self.subTest(case=invalid.index(profile)), patch("openvpn_addon.profile_check._openssl") as openssl:
                self.assertEqual(2, self.check(profile)[1])
                openssl.assert_not_called()

    def test_rejects_multiple_certificates_inside_one_block(self):
        cert = (self.root / "pki/ca.crt").read_text()
        self.assertEqual(2, self.check(self.profile.replace("</cert>", cert + "</cert>"))[1])

    def test_bad_pem_fails_without_output(self):
        start = self.profile.index("-----BEGIN PRIVATE KEY-----")
        end = self.profile.index("-----END PRIVATE KEY-----", start)
        bad = self.profile[:start] + "-----BEGIN PRIVATE KEY-----\nAAAA\n" + self.profile[end:]
        self.assert_failed(self.check(bad), "key_match")

    def test_unreadable_oversize_non_utf8_and_fifo_inputs_are_errors(self):
        self.assertEqual(2, check_profile(self.config, self.path)[1])
        self.assertEqual(2, self.check("x" * (MAX_PROFILE_BYTES + 1))[1])
        self.path.write_bytes(b"\xff")
        self.assertEqual(2, check_profile(self.config, self.path)[1])
        self.path.unlink()
        os.mkfifo(self.path)
        self.assertEqual(2, check_profile(self.config, self.path)[1])

    def test_tool_errors_and_timeouts_are_redacted(self):
        for error in (FileNotFoundError("secret-path"), subprocess.TimeoutExpired("secret-command", 5, output=b"secret-output")):
            with patch("openvpn_addon.profile_check._openssl", side_effect=error):
                report, code = self.check()
            self.assertEqual(2, code)
            self.assertNotIn("secret", json.dumps(report))

    def test_cli_checks_a_real_export(self):
        self.path.write_text(self.profile)
        args = build_parser().parse_args(["client", "check-profile", str(self.path), "--experimental-profile-check"])
        with patch("openvpn_addon.main.load_and_validate", return_value=self.config), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, args.func(args))
        self.assertEqual("pass", json.loads(output.getvalue())["status"])


if __name__ == "__main__":
    unittest.main()
