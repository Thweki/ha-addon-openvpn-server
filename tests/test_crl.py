import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import openvpn_addon.config as config_module
from openvpn_addon.crl import inspect_crl, render_crl
from openvpn_addon.main import build_parser


@unittest.skipUnless(shutil.which("openssl"), "OpenSSL is unavailable")
class CrlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.fixture.cleanup)
        root = Path(cls.fixture.name)

        def openssl(*args):
            # Capture all output: fixture generation must never print key material.
            result = subprocess.run(["openssl", *args], cwd=root, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError("OpenSSL CRL fixture generation failed")

        openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=Test CA",
                "-keyout", "ca.key", "-out", "ca.crt", "-days", "3650")
        (root / "ca.cnf").write_text(
            "[ca]\ndefault_ca=local\n[local]\n"
            "database=index.txt\nprivate_key=ca.key\ncertificate=ca.crt\n"
            "default_md=sha256\ndefault_crl_days=30\n"
        )
        # Deliberately not in serial order, with a revoked server, a reason,
        # and two historical serials for the same CN. No client keys needed.
        (root / "index.txt").write_text(
            "R\t290101000000Z\t250102030405Z,keyCompromise\tFF\tunknown\t/CN=alice\n"
            "V\t290101000000Z\t\t10\tunknown\t/CN=active\n"
            "R\t290101000000Z\t250203040506Z\t01\tunknown\t/CN=server\n"
            "R\t290101000000Z\t250304050607Z\tA0\tunknown\t/CN=alice\n"
        )
        openssl("ca", "-config", "ca.cnf", "-gencrl", "-out", "crl.pem",
                "-crl_lastupdate", "20250101000000Z", "-crl_nextupdate", "20990101000000Z")
        cls.pem = (root / "crl.pem").read_bytes()
        cls.ca = (root / "ca.crt").read_bytes()
        openssl("crl", "-in", "crl.pem", "-badsig", "-out", "bad.pem")
        cls.bad_pem = (root / "bad.pem").read_bytes()
        (root / "index.txt").write_text("")
        openssl("ca", "-config", "ca.cnf", "-gencrl", "-out", "empty.pem",
                "-crl_lastupdate", "20250101000000Z", "-crl_nextupdate", "20990101000000Z")
        cls.empty_pem = (root / "empty.pem").read_bytes()
        openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=Test CA",
                "-keyout", "other.key", "-out", "other.crt", "-days", "3650")
        cls.other_ca = (root / "other.crt").read_bytes()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.pki = self.root / "pki"
        self.pki.mkdir()
        (self.pki / "crl.pem").write_bytes(self.pem)
        (self.pki / "ca.crt").write_bytes(self.ca)
        self.now = datetime(2026, 6, 1, tzinfo=timezone.utc)

    def report(self, **kwargs):
        return inspect_crl(self.pki, now=kwargs.get("now", self.now))

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
                for p in self.root.rglob("*") if p.is_file()}

    def test_reports_actual_signed_revocations_without_index_or_keys(self):
        report = self.report()
        self.assertTrue(report["signature_verified"])
        self.assertEqual("current", report["status"])
        self.assertEqual("2025-01-01T00:00:00Z", report["last_update"])
        self.assertEqual("2099-01-01T00:00:00Z", report["next_update"])
        self.assertEqual(3, report["revoked_count"])
        self.assertEqual([
            {"serial": "01", "revoked_at": "2025-02-03T04:05:06Z"},
            {"serial": "A0", "revoked_at": "2025-03-04T05:06:07Z"},
            {"serial": "FF", "revoked_at": "2025-01-02T03:04:05Z"},
        ], report["revoked_certificates"])

    def test_ignores_index_and_does_not_mutate_state(self):
        (self.pki / "index.txt").write_text("not the source of CRL truth\n")
        before = self.snapshot()
        self.assertEqual(3, self.report()["revoked_count"])
        self.assertEqual(before, self.snapshot())
        self.assertEqual({"pki"}, {p.name for p in self.root.iterdir()})

    def test_empty_signed_crl_is_an_explicit_zero(self):
        (self.pki / "crl.pem").write_bytes(self.empty_pem)
        report = self.report()
        self.assertEqual(0, report["revoked_count"])
        self.assertEqual([], report["revoked_certificates"])

    def test_validity_boundaries(self):
        for date, expected in [
            ((2024, 12, 31), "not_yet_valid"),
            ((2025, 1, 1), "current"),
            ((2099, 1, 1), "expired"),
        ]:
            with self.subTest(expected=expected):
                self.assertEqual(expected, self.report(now=datetime(*date, tzinfo=timezone.utc))["status"])

    def test_naive_time_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "timezone"):
            self.report(now=datetime(2026, 1, 1))

    def test_wrong_ca_and_bad_signature_are_rejected(self):
        for crl, ca in [(self.pem, self.other_ca), (self.bad_pem, self.ca)]:
            with self.subTest(bad_signature=crl == self.bad_pem):
                (self.pki / "crl.pem").write_bytes(crl)
                (self.pki / "ca.crt").write_bytes(ca)
                with self.assertRaisesRegex(ValueError, "Cannot verify CRL"):
                    self.report()

    def test_missing_or_invalid_ca_is_rejected(self):
        (self.pki / "ca.crt").unlink()
        with self.assertRaisesRegex(ValueError, "Cannot verify CRL"):
            self.report()
        (self.pki / "ca.crt").write_text("invalid certificate")
        with self.assertRaisesRegex(ValueError, "Cannot verify CRL"):
            self.report()

    def test_missing_crl_does_not_initialize_pki(self):
        shutil.rmtree(self.pki)
        with self.assertRaisesRegex(ValueError, "CRL is missing"):
            self.report()
        self.assertFalse(self.pki.exists())

    def test_malformed_input_is_not_echoed(self):
        for contents in [b"PRIVATE_INPUT_SENTINEL", b"\xff"]:
            (self.pki / "crl.pem").write_bytes(contents)
            with self.assertRaises(ValueError) as raised:
                self.report()
            self.assertNotIn("PRIVATE_INPUT_SENTINEL", str(raised.exception))

    def test_formats_only_expose_public_report_fields(self):
        report = self.report()
        self.assertEqual(report, json.loads(render_crl(report, "json")))
        text = render_crl(report, "text")
        self.assertIn("Revoked certificates: 3", text)
        self.assertIn("serial=FF\trevoked_at=2025-01-02T03:04:05Z", text)
        self.assertNotIn("alice", text)
        self.assertNotIn("BEGIN", text)

    def command(self, *options):
        args = build_parser().parse_args(["crl", "show", *options])
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(config_module, "DATA_ROOT", self.root), \
                patch("openvpn_addon.main.load_config", side_effect=AssertionError("must not read options")), \
                patch("openvpn_addon.main.ensure_dirs", side_effect=AssertionError("must not initialize")), \
                patch("openvpn_addon.main.PkiManager", side_effect=AssertionError("must not mutate PKI")), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = args.func(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_text_and_json_are_read_only(self):
        before = self.snapshot()
        code, stdout, stderr = self.command()
        self.assertEqual(0, code)
        self.assertIn("CRL signature: verified", stdout)
        self.assertEqual("", stderr)
        code, stdout, stderr = self.command("--format", "json")
        self.assertEqual(0, code)
        self.assertEqual(3, json.loads(stdout)["revoked_count"])
        self.assertEqual("", stderr)
        self.assertEqual(before, self.snapshot())

    def test_cli_errors_go_to_stderr_without_a_partial_report(self):
        (self.pki / "crl.pem").write_text("PRIVATE_INPUT_SENTINEL")
        code, stdout, stderr = self.command("--format", "json")
        self.assertEqual(1, code)
        self.assertEqual("", stdout)
        self.assertIn("CRL inspection failed", stderr)
        self.assertNotIn("PRIVATE_INPUT_SENTINEL", stderr)

    def test_cli_expired_crl_prints_report_and_fails(self):
        report = self.report(now=datetime(2099, 1, 1, tzinfo=timezone.utc))
        with patch("openvpn_addon.main.inspect_crl", return_value=report):
            code, stdout, stderr = self.command("--format", "json")
        self.assertEqual(1, code)
        self.assertEqual("expired", json.loads(stdout)["status"])
        self.assertEqual("", stderr)


class CrlFailureTests(unittest.TestCase):
    def test_openssl_unavailable_or_timeout_is_sanitized(self):
        for error in [FileNotFoundError(), PermissionError(), subprocess.TimeoutExpired("openssl", 10)]:
            with self.subTest(error=type(error).__name__), \
                    patch.object(Path, "read_text", return_value="unused"), \
                    patch("openvpn_addon.crl.subprocess.run", side_effect=error):
                with self.assertRaisesRegex(ValueError, "OpenSSL"):
                    inspect_crl(Path("unused"))

    def test_zero_exit_without_verification_success_is_rejected(self):
        with patch.object(Path, "read_text", return_value="unused"), \
                patch("openvpn_addon.crl.subprocess.run", return_value=subprocess.CompletedProcess(
                    [], 0, "", "verify failure\n")):
            with self.assertRaisesRegex(ValueError, "Cannot verify CRL"):
                inspect_crl(Path("unused"))

    def test_incomplete_metadata_is_not_reported_as_empty(self):
        for text in ["", "lastUpdate=invalid\nnextUpdate=invalid\nNo Revoked Certificates."]:
            with self.subTest(text=text), patch.object(Path, "read_text", return_value="unused"), \
                    patch("openvpn_addon.crl.subprocess.run", return_value=subprocess.CompletedProcess(
                        [], 0, text, "verify OK\n")):
                with self.assertRaisesRegex(ValueError, "Cannot parse"):
                    inspect_crl(Path("unused"))


if __name__ == "__main__":
    unittest.main()
