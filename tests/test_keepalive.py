import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openvpn_addon import config as config_module, main, renderer
from openvpn_addon.config import AddonConfig
from openvpn_addon.keepalive import collect_keepalive


REPO = Path(__file__).resolve().parent.parent


class KeepaliveTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.path = self.root / "server.conf"

    def report(self, text):
        self.path.write_text(text, encoding="utf-8")
        return collect_keepalive(self.path)

    def test_real_template_expands_default_and_custom_timers(self):
        with patch.object(renderer, "TEMPLATES_DIR", REPO / "addon/templates"):
            for protocol in ("udp", "tcp"):
                for interval, timeout in ((10, 120), (20, 90)):
                    with self.subTest(protocol=protocol, interval=interval):
                        rendered = renderer.render_server_config(
                            AddonConfig(protocol=protocol, keepalive=f"{interval} {timeout}")
                        )
                        report = self.report(rendered)
                        self.assertEqual([], report["warnings"])
                        self.assertEqual(1, len(report["directives"]))
                        entry = report["directives"][0]
                        self.assertEqual("keepalive", entry["directive"])
                        self.assertEqual([interval, timeout], entry["values_seconds"])
                        self.assertEqual(f"keepalive {interval} {timeout}", rendered.splitlines()[entry["line"] - 1])
                        self.assertEqual({
                            "server": {"ping": interval, "ping-restart": timeout * 2},
                            "client_push": {"ping": interval, "ping-restart": timeout},
                        }, entry["server_mode_expansion"])

    def test_explicit_and_pushed_timers_with_comments_and_quotes(self):
        report = self.report(
            '# keepalive 1 2\n; ping 99\n  --ping 8 # comment\n'
            'ping-restart 60\npush "ping 12" ; comment\n'
            "push 'ping-restart 80'\npush \"ping-exit 100\"\n"
            'push "dhcp-option DNS 1.1.1.1"\n'
        )
        self.assertEqual([], report["warnings"])
        self.assertEqual([
            (3, "server", "ping", [8]),
            (4, "server", "ping-restart", [60]),
            (5, "client_push", "ping", [12]),
            (6, "client_push", "ping-restart", [80]),
            (7, "client_push", "ping-exit", [100]),
        ], [(e["line"], e["scope"], e["directive"], e["values_seconds"]) for e in report["directives"]])

    def test_zero_explicit_timers_are_preserved(self):
        report = self.report('ping 0\npush "ping-restart 0"\n')
        self.assertEqual([[0], [0]], [e["values_seconds"] for e in report["directives"]])
        self.assertEqual([], report["warnings"])

    def test_conflicts_and_duplicates_are_not_silently_resolved(self):
        report = self.report('keepalive 10 120\nping 5\npush "ping 6"\npush "ping 7"\n')
        self.assertEqual(4, len(report["directives"]))
        self.assertTrue(any("repeated ping" in w for w in report["warnings"]))
        self.assertTrue(any("coexist" in w for w in report["warnings"]))

    def test_invalid_and_disabled_helpers_have_no_expansion(self):
        for value in ("0 0", "0 120", "10 0", "10 19", "10 2147483647"):
            with self.subTest(value=value):
                report = self.report(f"keepalive {value}\n")
                self.assertNotIn("server_mode_expansion", report["directives"][0])
                self.assertTrue(report["warnings"])

    def test_keepalive_cannot_be_pushed_as_a_helper(self):
        report = self.report('push "keepalive 10 120"\n')
        self.assertNotIn("server_mode_expansion", report["directives"][0])
        self.assertIn("not a supported client push", report["warnings"][0])

    def test_malformed_timers_are_omitted_without_echoing_input(self):
        for line in (
            "keepalive 10", "ping -1", "ping 1.5", "ping 10 extra",
            "ping 2147483648", "ping " + "9" * 5000,
            'push "ping 10', 'push ping 10', 'push "ping-restart secret-canary"',
            "keepalive secret-canary 120", 'ping "secret-canary',
        ):
            with self.subTest(line_type=line.split()[0]):
                report = self.report(line + "\nping 11\n")
                self.assertEqual([[11]], [e["values_seconds"] for e in report["directives"]])
                self.assertIn("Line 1: malformed", report["warnings"][0])
                self.assertNotIn("secret-canary", json.dumps(report))

    def test_inline_blocks_and_unrelated_values_are_never_reported(self):
        report = self.report(
            'key /private/secret-canary.key\n<key>\nsecret-canary\nping 999\n</key>\n'
            '<tls-crypt>\npush "ping 888"\n</tls-crypt>\n'
            '<cert>secret-canary</cert>\nauth-user-pass secret-canary\n'
            'push "unrelated secret-canary"\nkeepalive 10 120\n'
        )
        self.assertEqual(1, len(report["directives"]))
        self.assertEqual("keepalive", report["directives"][0]["directive"])
        self.assertNotIn("secret-canary", json.dumps(report))

    def test_unterminated_inline_block_is_not_parsed(self):
        report = self.report('<key>\nping 999\n')
        self.assertEqual([], report["directives"])
        self.assertTrue(any("Unterminated" in w for w in report["warnings"]))

    def test_includes_and_push_modifiers_warn_without_following_paths(self):
        report = self.report('config secret-canary\npush-reset\npush-remove secret-canary\nping 10\n')
        self.assertEqual(3, len(report["warnings"]))
        self.assertNotIn("secret-canary", json.dumps(report))

    def test_no_timers_does_not_infer_defaults(self):
        report = self.report("proto udp\n")
        self.assertTrue(report["available"])
        self.assertEqual([], report["directives"])
        self.assertIn("No supported timer", report["warnings"][0])

    def test_missing_unreadable_and_non_utf8_files(self):
        self.assertFalse(collect_keepalive(self.path)["available"])
        self.assertFalse(self.path.exists())
        with patch.object(Path, "read_text", side_effect=PermissionError("secret-canary")):
            report = collect_keepalive(self.path)
        self.assertFalse(report["available"])
        self.assertNotIn("secret-canary", json.dumps(report))
        self.path.write_bytes(b"ping 10\n\xff")
        self.assertFalse(collect_keepalive(self.path)["available"])

    def test_only_reads_server_config_and_does_not_change_it(self):
        content = 'key secret-canary\nconfig secret-canary\nkeepalive 10 120\n'
        self.path.write_text(content, encoding="utf-8")
        original_open = Path.open
        opened = []

        def guarded_open(path, mode="r", *args, **kwargs):
            self.assertEqual(self.path, path)
            self.assertEqual("r", mode)
            opened.append(path)
            return original_open(path, mode, *args, **kwargs)

        with patch.object(Path, "open", guarded_open):
            collect_keepalive(self.path)
        self.assertEqual([self.path], opened)
        self.assertEqual(content, self.path.read_text(encoding="utf-8"))
        self.assertEqual([self.path], list(self.root.iterdir()))

    def test_cli_json_and_exit_codes_without_loading_options_or_initializing(self):
        with patch.object(config_module, "DATA_ROOT", self.root):
            path = AddonConfig().server_conf_path
            for exists in (False, True):
                with self.subTest(exists=exists):
                    if exists:
                        path.parent.mkdir()
                        path.write_text("keepalive 10 120\n", encoding="utf-8")
                    with (
                        patch.object(main, "load_and_validate", side_effect=AssertionError("loads options")),
                        patch.object(main, "ensure_dirs", side_effect=AssertionError("creates directories")),
                        patch.object(main, "PkiManager", side_effect=AssertionError("reads PKI")),
                        patch.object(main, "apply_network", side_effect=AssertionError("changes network")),
                        contextlib.redirect_stdout(io.StringIO()) as output,
                    ):
                        args = main.build_parser().parse_args(["keepalive"])
                        code = args.func(args)
                    self.assertEqual(0 if exists else 1, code)
                    self.assertEqual(exists, json.loads(output.getvalue())["available"])

    def test_module_entrypoint_reports_rendered_values_despite_invalid_options(self):
        runtime = self.root / "runtime"
        runtime.mkdir()
        (runtime / "server.conf").write_text("keepalive 15 100\n", encoding="utf-8")
        options = self.root / "options.json"
        options.write_text("invalid secret-canary", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "openvpn_addon.main", "keepalive"],
            env={**os.environ, "PYTHONPATH": str(REPO / "addon/src"),
                 "OPENVPN_ADDON_DATA": str(self.root), "OPENVPN_ADDON_OPTIONS": str(options)},
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stderr)
        self.assertEqual([15, 100], json.loads(result.stdout)["directives"][0]["values_seconds"])
        self.assertNotIn("secret-canary", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
