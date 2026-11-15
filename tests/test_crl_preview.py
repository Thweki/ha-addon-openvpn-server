import contextlib
import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import openvpn_addon.config as config_module
from openvpn_addon.config import AddonConfig
from openvpn_addon.crl_preview import preview_crl_renewal
from openvpn_addon.main import build_parser


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


class PreviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        root_patch = patch.object(config_module, "DATA_ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.config = AddonConfig()
        self.config.pki_dir.mkdir()
        (self.config.pki_dir / "index.txt").write_text("")
        self.config.crl_path.write_text("mock CRL")

    def preview(self, remaining, days=3650):
        self.config.crl_valid_days = days
        expiry = NOW + timedelta(seconds=remaining)
        output = expiry.strftime("nextUpdate=%b %d %H:%M:%S %Y GMT\n")
        with patch("openvpn_addon.crl_preview.datetime") as clock, \
             patch("openvpn_addon.crl_preview.subprocess.run", return_value=Mock(stdout=output)) as run:
            clock.now.return_value = NOW
            clock.fromtimestamp.side_effect = datetime.fromtimestamp
            result = preview_crl_renewal(self.config, experimental=True)
        run.assert_called_once_with(
            ["openssl", "crl", "-in", str(self.config.crl_path), "-noout", "-nextupdate"],
            capture_output=True, text=True, check=True, timeout=30, stdin=subprocess.DEVNULL,
        )
        return result

    def test_default_and_non_boolean_opt_in_refuse_before_io(self):
        with patch.object(Path, "is_file") as stat, \
             patch("openvpn_addon.crl_preview.subprocess.run") as run:
            with self.assertRaises(ValueError):
                preview_crl_renewal(self.config)
            for value in (False, None, 1, "true"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    preview_crl_renewal(self.config, experimental=value)
            stat.assert_not_called()
            run.assert_not_called()

    def test_seven_day_boundary_and_expiry(self):
        for remaining, action, reason in (
            (604801, "keep", "outside_renewal_window"),
            (604800, "renew", "within_renewal_window"),
            (1, "renew", "within_renewal_window"),
            (0, "renew", "expired"),
            (-1, "renew", "expired"),
        ):
            with self.subTest(remaining=remaining):
                result = self.preview(remaining)
                self.assertEqual(action, result["action"])
                self.assertEqual(reason, result["reason"])
                self.assertTrue(result["dry_run"])
                self.assertEqual(604800, result["renewal_window_seconds"])
                self.assertEqual(
                    (NOW + timedelta(seconds=remaining - 604800)).isoformat(),
                    result["renewal_due_at"],
                )
                self.assertEqual(
                    (NOW + timedelta(days=3650)).isoformat() if action == "renew" else None,
                    result["proposed_next_update"],
                )

    def test_short_lifetime_uses_half_life(self):
        self.assertEqual("keep", self.preview(43201, days=1)["action"])
        result = self.preview(43200, days=1)
        self.assertEqual("renew", result["action"])
        self.assertEqual(43200, result["renewal_window_seconds"])
        self.assertEqual((NOW + timedelta(days=1)).isoformat(), result["proposed_next_update"])

    def test_missing_crl_plans_generation_without_running_any_command(self):
        self.config.crl_path.unlink()
        with patch("openvpn_addon.crl_preview.subprocess.run") as run:
            result = preview_crl_renewal(self.config, experimental=True)
            run.assert_not_called()
        self.assertEqual("renew", result["action"])
        self.assertEqual("missing_crl", result["reason"])
        self.assertIsNone(result["renewal_due_at"])
        self.assertIsNotNone(result["proposed_next_update"])
        self.assertFalse(self.config.crl_path.exists())

    def test_missing_index_blocks_even_if_crl_missing(self):
        (self.config.pki_dir / "index.txt").unlink()
        for missing in (False, True):
            if missing:
                self.config.crl_path.unlink()
            with patch("openvpn_addon.crl_preview.subprocess.run") as run:
                result = preview_crl_renewal(self.config, experimental=True)
                run.assert_not_called()
            self.assertEqual("blocked", result["action"])
            self.assertEqual("missing_pki_index", result["reason"])

    def test_invalid_lifetimes_block_before_pki_access(self):
        for days in (0, -1, True, "3650", 10**20):
            self.config.crl_valid_days = days
            with self.subTest(days=days), patch.object(Path, "is_file") as stat:
                result = preview_crl_renewal(self.config, experimental=True)
                self.assertEqual("blocked", result["action"])
                self.assertEqual("invalid_crl_valid_days", result["reason"])
                stat.assert_not_called()

    def test_bad_crl_metadata_blocks_without_echoing_output(self):
        for output in ("", "nextUpdate=INVALID_TEST_VALUE", "lastUpdate=Jan 1 00:00:00 2030 GMT"):
            with self.subTest(output=output), patch(
                "openvpn_addon.crl_preview.subprocess.run", return_value=Mock(stdout=output)
            ):
                result = preview_crl_renewal(self.config, experimental=True)
                self.assertEqual("blocked", result["action"])
                self.assertEqual("cannot_inspect_crl_or_index", result["reason"])
                self.assertNotIn("INVALID_TEST_VALUE", json.dumps(result))

    def test_subprocess_errors_are_redacted_and_blocked(self):
        errors = [
            FileNotFoundError("PRIVATE_TEST_SENTINEL"),
            subprocess.CalledProcessError(1, "openssl", output="PRIVATE_TEST_SENTINEL"),
            subprocess.TimeoutExpired("openssl", 30, output="PRIVATE_TEST_SENTINEL"),
        ]
        for error in errors:
            with self.subTest(error=type(error)), patch(
                "openvpn_addon.crl_preview.subprocess.run", side_effect=error
            ):
                result = preview_crl_renewal(self.config, experimental=True)
                self.assertEqual("blocked", result["action"])
                self.assertNotIn("PRIVATE_TEST_SENTINEL", json.dumps(result))

    def test_stat_failure_is_not_treated_as_missing_crl(self):
        with patch.object(Path, "stat", side_effect=PermissionError("PRIVATE_TEST_SENTINEL")):
            result = preview_crl_renewal(self.config, experimental=True)
        self.assertEqual("blocked", result["action"])
        self.assertIsNone(result["proposed_next_update"])

    def invoke(self, *arguments):
        args = build_parser().parse_args(["crl-renew-preview", *arguments])
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = args.func(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_default_off_does_not_load_options_or_pki(self):
        with patch("openvpn_addon.main.load_config") as load, \
             patch("openvpn_addon.main.preview_crl_renewal") as preview:
            code, stdout, stderr = self.invoke()
        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertIn("--experimental", stderr)
        load.assert_not_called()
        preview.assert_not_called()

    def test_cli_json_and_exit_codes(self):
        self.config.crl_path.unlink()
        with patch("openvpn_addon.main.load_config", return_value=self.config):
            code, stdout, stderr = self.invoke("--experimental")
            self.assertEqual(0, code)
            self.assertEqual("", stderr)
            self.assertEqual("renew", json.loads(stdout)["action"])
            (self.config.pki_dir / "index.txt").unlink()
            code, stdout, stderr = self.invoke("--experimental")
            self.assertEqual(2, code)
            self.assertEqual("blocked", json.loads(stdout)["action"])

    def test_cli_invalid_options_do_not_echo_values(self):
        with patch("openvpn_addon.main.load_config", side_effect=ValueError("PRIVATE_TEST_SENTINEL")):
            code, stdout, stderr = self.invoke("--experimental")
        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertNotIn("PRIVATE_TEST_SENTINEL", stderr)

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL unavailable")
    def test_real_crl_preview_preserves_files_and_does_not_read_keys(self):
        private = self.config.pki_dir / "private"
        private.mkdir()
        ca_key = private / "ca.key"
        ca_cert = self.config.pki_dir / "ca.crt"
        ca_config = self.root / "openssl.cnf"
        ca_config.write_text(
            "[ca]\ndefault_ca=CA\n[CA]\n"
            f"database={self.config.pki_dir / 'index.txt'}\n"
            f"private_key={ca_key}\ncertificate={ca_cert}\n"
            "default_md=sha256\ndefault_crl_days=1\n"
        )
        def openssl(*args):
            # Test fixtures only; never print generated key material or subprocess diagnostics.
            subprocess.run(["openssl", *args], check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=30)
        openssl("req", "-new", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                "-nodes", "-subj", "/CN=preview-test", "-keyout", str(ca_key), "-out", str(ca_cert))
        openssl("ca", "-gencrl", "-config", str(ca_config), "-out", str(self.config.crl_path))

        def snapshot():
            return {
                str(p.relative_to(self.root)): (
                    hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None,
                    p.stat().st_mtime_ns, p.stat().st_mode,
                )
                for p in self.root.rglob("*")
            }
        before = snapshot()
        real_open = Path.open
        def guarded_open(path, *args, **kwargs):
            self.assertNotIn(private, path.parents, "Preview tried to read private material")
            return real_open(path, *args, **kwargs)
        real_run = subprocess.run
        with patch.object(Path, "open", guarded_open), \
             patch("openvpn_addon.crl_preview.subprocess.run", wraps=real_run) as run, \
             patch("openvpn_addon.main.load_config", return_value=self.config), \
             patch("openvpn_addon.main.ensure_dirs") as ensure_dirs, \
             patch("openvpn_addon.main.PkiManager") as manager:
            code, stdout, stderr = self.invoke("--experimental")
            self.assertEqual(0, code)
            self.assertEqual("renew", json.loads(stdout)["action"])
            self.config.crl_valid_days = 1
            code, stdout, stderr = self.invoke("--experimental")
            self.assertEqual(0, code)
            self.assertEqual("keep", json.loads(stdout)["action"])
            self.assertEqual(2, run.call_count)
            for call in run.call_args_list:
                self.assertEqual(
                    ["openssl", "crl", "-in", str(self.config.crl_path), "-noout", "-nextupdate"],
                    call.args[0],
                )
            ensure_dirs.assert_not_called()
            manager.assert_not_called()
        self.assertEqual(before, snapshot())


if __name__ == "__main__":
    unittest.main()
