import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from openvpn_addon.expiry import collect_expiry, expiry_text
from openvpn_addon.main import build_parser, cmd_expiry


NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)


def dates(end, start=NOW - timedelta(days=365)):
    return subprocess.CompletedProcess(
        [], 0,
        f"notBefore={start.strftime('%b %d %H:%M:%S %Y GMT')}\n"
        f"notAfter={end.strftime('%b %d %H:%M:%S %Y GMT')}\n",
    )


class ExpiryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pki = self.root / "pki"
        (self.pki / "issued").mkdir(parents=True)
        self.ca = self.pki / "ca.crt"
        self.server = self.pki / "issued" / "server.crt"
        self.ca.write_text("certificate placeholder")
        self.server.write_text("certificate placeholder")

    def report(self, ca_end, server_end, **kwargs):
        with patch("openvpn_addon.expiry.subprocess.run", side_effect=[dates(ca_end), dates(server_end)]):
            return collect_expiry(self.pki, now=NOW, **kwargs)

    def test_ca_can_limit_a_healthy_server(self):
        report = self.report(NOW + timedelta(days=10), NOW + timedelta(days=100))
        self.assertEqual("warning", report["status"])
        self.assertEqual(["ca"], report["limiting_certificates"])
        self.assertEqual((NOW + timedelta(days=10)).isoformat(), report["expiry_deadline"])
        self.assertEqual(["expiring", "ok"], [item["status"] for item in report["certificates"]])

    def test_server_can_limit_ca_and_ties_are_retained(self):
        end = NOW + timedelta(days=100)
        self.assertEqual(["server"], self.report(end, end - timedelta(days=1))["limiting_certificates"])
        report = self.report(end, end)
        self.assertEqual(["ca", "server"], report["limiting_certificates"])
        self.assertEqual("ok", report["status"])

    def test_exact_time_boundaries_not_rounded_days(self):
        cases = [
            (timedelta(days=30, seconds=1), "ok", 30),
            (timedelta(days=30), "expiring", 30),
            (timedelta(seconds=1), "expiring", 0),
            (timedelta(0), "expired", 0),
            (timedelta(seconds=-1), "expired", -1),
        ]
        for delta, status, days in cases:
            with self.subTest(delta=delta):
                report = self.report(NOW + delta, NOW + timedelta(days=100))
                self.assertEqual(status, report["certificates"][0]["status"])
                self.assertEqual(days, report["certificates"][0]["days_remaining"])

    def test_custom_and_zero_warning_windows(self):
        end = NOW + timedelta(days=60)
        self.assertEqual("warning", self.report(end, end, within_days=90)["status"])
        self.assertEqual("ok", self.report(NOW + timedelta(seconds=1), end, within_days=0)["status"])
        self.assertEqual("error", self.report(NOW, end, within_days=0)["status"])

    def test_not_yet_valid_is_error_even_with_distant_expiry(self):
        with patch("openvpn_addon.expiry.subprocess.run", return_value=dates(NOW + timedelta(days=100), NOW + timedelta(seconds=1))):
            report = collect_expiry(self.pki, now=NOW)
        self.assertEqual("error", report["status"])
        self.assertEqual("not_yet_valid", report["certificates"][0]["status"])

    def test_validity_starts_at_check_time_and_clock_is_normalized_to_utc(self):
        local_now = NOW.astimezone(timezone(timedelta(hours=-5)))
        with patch("openvpn_addon.expiry.subprocess.run", return_value=dates(NOW + timedelta(days=100), NOW)):
            report = collect_expiry(self.pki, now=local_now)
        self.assertEqual(NOW.isoformat(), report["checked_at"])
        self.assertEqual("ok", report["status"])

    def test_missing_pki_is_reported_without_creating_it(self):
        missing = self.root / "absent"
        with patch("openvpn_addon.expiry.subprocess.run") as run:
            report = collect_expiry(missing, now=NOW)
        run.assert_not_called()
        self.assertFalse(missing.exists())
        self.assertEqual(["missing", "missing"], [item["status"] for item in report["certificates"]])
        self.assertEqual("error", report["status"])
        self.assertIsNone(report["expiry_deadline"])
        self.assertEqual([], report["limiting_certificates"])

    def test_one_missing_certificate_does_not_hide_the_other(self):
        self.ca.unlink()
        with patch("openvpn_addon.expiry.subprocess.run", return_value=dates(NOW - timedelta(days=1))):
            report = collect_expiry(self.pki, now=NOW)
        self.assertEqual(["missing", "expired"], [item["status"] for item in report["certificates"]])
        self.assertIsNone(report["expiry_deadline"])

    def test_openssl_failures_are_sanitized(self):
        marker = "SENSITIVE_TEST_MARKER"
        failures = [
            subprocess.CompletedProcess([], 1, marker, marker),
            FileNotFoundError(marker),
            PermissionError(marker),
            subprocess.TimeoutExpired(marker, 5, output=marker, stderr=marker),
            subprocess.CompletedProcess([], 0, "notAfter=invalid date\n"),
            dates(NOW, NOW + timedelta(days=1)),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                kwargs = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with patch("openvpn_addon.expiry.subprocess.run", **kwargs):
                    report = collect_expiry(self.pki, now=NOW)
                self.assertEqual("error", report["status"])
                self.assertEqual("unreadable", report["certificates"][0]["status"])
                self.assertIsNone(report["expiry_deadline"])
                self.assertNotIn(marker, json.dumps(report) + expiry_text(report))

    def test_symlink_and_non_file_are_not_opened(self):
        self.ca.unlink()
        self.ca.symlink_to(self.root / "private.key")
        self.server.unlink()
        self.server.mkdir()
        with patch("openvpn_addon.expiry.subprocess.run") as run:
            report = collect_expiry(self.pki, now=NOW)
        run.assert_not_called()
        self.assertEqual(["unreadable", "unreadable"], [item["status"] for item in report["certificates"]])

    def test_only_public_ca_and_server_files_are_inspected(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with patch("openvpn_addon.expiry.subprocess.run", return_value=dates(NOW + timedelta(days=100))) as run:
            collect_expiry(self.pki, now=NOW)
        self.assertEqual(2, run.call_count)
        for call, path in zip(run.call_args_list, [self.ca, self.server]):
            self.assertEqual(["openssl", "x509", "-in", str(path), "-noout", "-startdate", "-enddate"], call.args[0])
            self.assertEqual({"capture_output": True, "text": True, "timeout": 5}, call.kwargs)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_invalid_arguments_fail_before_reading(self):
        with patch("openvpn_addon.expiry.subprocess.run") as run:
            with self.assertRaises(ValueError):
                collect_expiry(self.pki, -1)
            with self.assertRaises(ValueError):
                collect_expiry(self.pki, now=datetime(2026, 1, 1))
        run.assert_not_called()
        for value in ("-1", "1.5", "many"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                build_parser().parse_args(["expiry", "--within-days", value])
            self.assertEqual(2, error.exception.code)

    def test_cli_formats_exit_codes_and_does_not_load_options_or_initialize(self):
        for days, expected_status, code in [(100, "ok", 0), (10, "warning", 1), (-1, "error", 2)]:
            for output_format in ("text", "json"):
                args = build_parser().parse_args(["expiry", "--within-days", "45", "--format", output_format])
                self.assertIs(args.func, cmd_expiry)
                output = io.StringIO()
                with (
                    patch("openvpn_addon.main.DATA_ROOT", self.root),
                    patch("openvpn_addon.main.load_config", side_effect=AssertionError("must not read options")),
                    patch("openvpn_addon.main.ensure_dirs", side_effect=AssertionError("must not initialize")),
                    patch("openvpn_addon.main.PkiManager", side_effect=AssertionError("must not change PKI")),
                    patch("openvpn_addon.expiry.subprocess.run", return_value=dates(datetime.now(timezone.utc) + timedelta(days=days))),
                    contextlib.redirect_stdout(output),
                ):
                    self.assertEqual(code, args.func(args))
                if output_format == "json":
                    payload = json.loads(output.getvalue())
                    self.assertEqual(expected_status, payload["status"])
                    self.assertEqual(45, payload["within_days"])
                else:
                    self.assertIn(f"CA/server certificate expiry: {expected_status}", output.getvalue())
                    self.assertIn("Shared expiry deadline:", output.getvalue())

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL is not installed")
    def test_real_openssl_certificates_and_cli_are_read_only(self):
        for path, days in ((self.ca, 10), (self.server, 100)):
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=test",
                 "-keyout", str(self.root / f"{days}.key"), "-out", str(path), "-days", str(days)],
                check=True, capture_output=True, timeout=30,
            )
        # Compare digests so a failed assertion can never print generated keys.
        def snapshot():
            return {
                p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
                for p in self.root.rglob("*") if p.is_file()
            }

        options = self.root / "options.json"
        options.write_text("intentionally invalid JSON")
        before = snapshot()
        report = collect_expiry(self.pki)
        self.assertEqual("warning", report["status"])
        self.assertEqual(["ca"], report["limiting_certificates"])
        self.assertEqual(["expiring", "ok"], [item["status"] for item in report["certificates"]])
        self.assertGreaterEqual(report["certificates"][0]["days_remaining"], 9)
        future = datetime.now(timezone.utc) + timedelta(days=90)
        self.assertEqual("error", collect_expiry(self.pki, now=future)["status"])
        env = dict(os.environ, OPENVPN_ADDON_DATA=str(self.root), OPENVPN_ADDON_OPTIONS=str(options))
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "addon" / "src")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        cli = subprocess.run(
            [sys.executable, "-m", "openvpn_addon.main", "expiry", "--format", "json"],
            env=env, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(1, cli.returncode)
        self.assertEqual("warning", json.loads(cli.stdout)["status"])
        self.assertEqual("", cli.stderr)
        self.assertEqual(before, snapshot())
        self.assertNotIn("PRIVATE KEY", json.dumps(report) + expiry_text(report))


if __name__ == "__main__":
    unittest.main()
