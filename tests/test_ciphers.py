import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openvpn_addon import config as config_module, main, renderer
from openvpn_addon.ciphers import collect_ciphers
from openvpn_addon.config import AddonConfig


class CipherReportTests(unittest.TestCase):
    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.root = Path(tempdir.name)
        self.path = self.root / "runtime" / "server.conf"
        self.path.parent.mkdir()

    def report(self, content):
        self.path.write_text(content, encoding="utf-8")
        return collect_ciphers(self.path)

    def values(self, report, directive, occurrence=0):
        return report["settings"][directive][occurrence]["values"]

    def test_shipped_template_reports_explicit_defaults_without_reading_pki(self):
        templates = Path(__file__).resolve().parent.parent / "addon" / "templates"
        with patch.object(renderer, "TEMPLATES_DIR", templates):
            report = self.report(renderer.render_server_config(AddonConfig()))
        self.assertTrue(report["available"])
        self.assertTrue(report["complete"])
        self.assertEqual([], report["warnings"])
        data = self.values(report, "data-ciphers")
        self.assertEqual(["AES-256-GCM", "AES-128-GCM", "CHACHA20-POLY1305"], [v["name"] for v in data])
        self.assertEqual(["modern"] * 3, [v["assessment"] for v in data])
        self.assertEqual("AES-256-GCM", self.values(report, "data-ciphers-fallback")[0]["name"])
        self.assertEqual("1.2", self.values(report, "tls-version-min")[0]["name"])
        self.assertEqual([], report["settings"]["tls-cipher"])
        self.assertEqual([], report["settings"]["tls-ciphersuites"])

    def test_weak_fallback_is_visible_alongside_modern_negotiation(self):
        report = self.report("data-ciphers AES-256-GCM\ndata-ciphers-fallback BF-CBC\n")
        self.assertEqual("modern", self.values(report, "data-ciphers")[0]["assessment"])
        self.assertEqual("deprecated", self.values(report, "data-ciphers-fallback")[0]["assessment"])

    def test_data_cipher_classification_and_optional_marker(self):
        report = self.report("data-ciphers ?CHACHA20-POLY1305:AES-256-CBC:BF-CBC:DES-EDE3-CBC:none:FUTURE-CIPHER\n")
        self.assertEqual(
            ["modern", "legacy", "deprecated", "deprecated", "deprecated", "unknown"],
            [v["assessment"] for v in self.values(report, "data-ciphers")],
        )

    def test_tls_versions_and_or_highest(self):
        for version, expected in [("1.0", "deprecated"), ("1.1", "deprecated"), ("1.2", "modern"), ("1.3", "modern"), ("9.9", "unknown")]:
            with self.subTest(version=version):
                report = self.report(f"tls-version-min {version} or-highest\ntls-version-max {version}\n")
                for directive in ("tls-version-min", "tls-version-max"):
                    self.assertEqual(expected, self.values(report, directive)[0]["assessment"])
                self.assertTrue(report["settings"]["tls-version-min"][0]["or_highest"])

    def test_tls_suites_and_expressions_are_not_confused(self):
        report = self.report(
            "tls-cipher ECDHE-RSA-AES256-GCM-SHA384:RC4-MD5:DEFAULT:!RC4:!aNULL:@SECLEVEL=2:TLS-RSA-WITH-AES-256-CBC-SHA:FUTURE:-RC4\n"
            "tls-ciphersuites TLS_AES_256_GCM_SHA384:TLS_CHACHA20_POLY1305_SHA256\n"
        )
        self.assertEqual(
            ["modern", "deprecated", "unknown", "unknown", "unknown", "unknown", "legacy", "unknown", "unknown"],
            [v["assessment"] for v in self.values(report, "tls-cipher")],
        )
        self.assertEqual(["modern", "modern"], [v["assessment"] for v in self.values(report, "tls-ciphersuites")])

    def test_comments_quotes_and_optional_directive_prefix(self):
        report = self.report(
            "# cipher BF-CBC\n ; cipher none\n"
            '  --data-ciphers "AES-256-GCM:?CHACHA20-POLY1305" # comment\n'
            "tls-version-min '1.2' ; comment\n"
        )
        self.assertEqual([], report["settings"]["cipher"])
        self.assertEqual(3, report["settings"]["data-ciphers"][0]["line"])
        self.assertEqual([], report["warnings"])

    def test_repeated_and_legacy_directives_are_preserved_with_warnings(self):
        report = self.report("cipher BF-CBC\nncp-ciphers AES-128-GCM\ndata-ciphers AES-256-GCM\ndata-ciphers AES-128-GCM\n")
        self.assertEqual("AES-256-GCM", self.values(report, "data-ciphers")[0]["name"])
        self.assertEqual("AES-128-GCM", self.values(report, "data-ciphers", 1)[0]["name"])
        self.assertTrue(any("repeated data-ciphers" in warning for warning in report["warnings"]))
        self.assertTrue(any("legacy cipher" in warning for warning in report["warnings"]))
        self.assertTrue(any("legacy ncp-ciphers" in warning for warning in report["warnings"]))

    def test_malformed_values_are_omitted_and_report_is_partial(self):
        report = self.report(
            'data-ciphers "unterminated\ndata-ciphers\ndata-ciphers AES-256-GCM extra\n'
            'data-ciphers AES-256-GCM::BF-CBC\ntls-cipher "/private/path"\n'
            "tls-version-min 1.2 unexpected\ndata-ciphers AES-128-GCM\n"
        )
        self.assertFalse(report["complete"])
        self.assertEqual(6, len(report["warnings"]))
        self.assertEqual(1, len(report["settings"]["data-ciphers"]))
        self.assertNotIn("unterminated", json.dumps(report))
        self.assertNotIn("/private/path", json.dumps(report))

    def test_ignores_inline_secrets_and_does_not_follow_file_references(self):
        report = self.report(
            "key /private/server.key\ntls-crypt /private/ta.key\n"
            "config /private/include.conf\n<key>\nPRIVATE-TEST-MARKER\ncipher BF-CBC\n</key>\n"
            "<tls-crypt>\nPRIVATE-TEST-MARKER\ndata-ciphers none\n</tls-crypt>\n"
            "<ca>PRIVATE-TEST-MARKER</ca>\ntls-version-min 1.2\n"
        )
        self.assertFalse(report["complete"])
        self.assertEqual([], report["settings"]["cipher"])
        self.assertEqual([], report["settings"]["data-ciphers"])
        self.assertNotIn("PRIVATE-TEST-MARKER", json.dumps(report))
        self.assertNotIn("/private/", json.dumps(report))
        original_open = Path.open
        opened = []

        def guarded_open(path, *args, **kwargs):
            self.assertEqual(self.path, path)
            self.assertNotIn("w", kwargs.get("mode", "r"))
            opened.append(path)
            return original_open(path, *args, **kwargs)

        before = self.path.read_bytes(), self.path.stat().st_mtime_ns
        with patch.object(Path, "open", guarded_open), patch("subprocess.Popen", side_effect=AssertionError("subprocess forbidden")):
            self.assertEqual(report, collect_ciphers(self.path))
        self.assertEqual([self.path], opened)
        self.assertEqual(before, (self.path.read_bytes(), self.path.stat().st_mtime_ns))

    def test_unterminated_inline_block_fails_closed(self):
        report = self.report("<key>\ncipher BF-CBC\ndata-ciphers none\n")
        self.assertFalse(report["complete"])
        self.assertTrue(all(not values for values in report["settings"].values()))
        self.assertIn("Unterminated inline block", report["warnings"][0])

    def test_unsupported_crypto_controls_are_flagged_without_echoing_arguments(self):
        report = self.report("compat-mode 2.3\nncp-disable\nsecret /private/file\n")
        self.assertFalse(report["complete"])
        self.assertEqual(3, len(report["warnings"]))
        self.assertNotIn("/private/file", json.dumps(report))

    def test_missing_and_unreadable_configuration_have_no_invented_defaults(self):
        for error in (FileNotFoundError(), PermissionError("PRIVATE-TEST-MARKER")):
            with self.subTest(error=type(error).__name__), patch.object(Path, "open", side_effect=error):
                report = collect_ciphers(self.path)
            self.assertFalse(report["available"])
            self.assertFalse(report["complete"])
            self.assertTrue(all(not values for values in report["settings"].values()))
            self.assertNotIn("PRIVATE-TEST-MARKER", json.dumps(report))

    def test_invalid_utf8_discards_partial_report(self):
        self.path.write_bytes(b"data-ciphers AES-256-GCM\n" + b"# padding\n" * 2000 + b"\xff")
        report = collect_ciphers(self.path)
        self.assertFalse(report["available"])
        self.assertEqual([], report["settings"]["data-ciphers"])

    def test_empty_file_is_explicitly_unconfigured(self):
        report = self.report("")
        self.assertTrue(report["available"])
        self.assertTrue(all(not values for values in report["settings"].values()))
        self.assertIn("defaults are not inferred", report["notes"][0])

    def test_cli_reads_disk_without_loading_options_or_creating_state(self):
        self.report("data-ciphers-fallback BF-CBC\n")
        output = io.StringIO()
        with patch.object(config_module, "DATA_ROOT", self.root), \
                patch.object(main, "load_config", side_effect=AssertionError("options forbidden")), \
                patch.object(main, "ensure_dirs", side_effect=AssertionError("writes forbidden")), \
                patch.object(main, "PkiManager", side_effect=AssertionError("PKI forbidden")), \
                patch("sys.argv", ["openvpn-addon", "ciphers"]), contextlib.redirect_stdout(output):
            self.assertEqual(0, main.main())
        self.assertEqual("deprecated", self.values(json.loads(output.getvalue()), "data-ciphers-fallback")[0]["assessment"])
        self.assertEqual([self.path.parent, self.path], sorted(self.root.rglob("*")))

    def test_cli_missing_file_returns_nonzero_json_and_creates_nothing(self):
        output = io.StringIO()
        with patch.object(config_module, "DATA_ROOT", self.root / "absent"), \
                patch("sys.argv", ["openvpn-addon", "ciphers"]), contextlib.redirect_stdout(output):
            self.assertEqual(1, main.main())
        self.assertFalse(json.loads(output.getvalue())["available"])
        self.assertFalse((self.root / "absent").exists())


if __name__ == "__main__":
    unittest.main()
