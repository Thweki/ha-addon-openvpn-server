import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openvpn_addon import config as config_module, main, renderer
from openvpn_addon.config import AddonConfig
from openvpn_addon.mtu import DIRECTIVES, collect_mtu


class MtuTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "server.conf"

    def report(self, text):
        self.path.write_text(text, encoding="utf-8")
        return collect_mtu(self.path)

    def test_numeric_settings_and_pushes_have_source_lines(self):
        report = self.report(
            '# ignored\n--tun-mtu "1400" # comment\ntun-mtu-extra 32\n'
            'link-mtu 1450 ; comment\nmssfix 1300 mtu\nfragment 1350 mtu\n'
            'push "tun-mtu 1400"\npush \'mssfix 1200 fixed\'\nmax-packet-size 1250\n'
        )
        self.assertTrue(report["complete"])
        server, pushed = report["settings"]["server"], report["settings"]["push"]
        for name, line, args in (
            ("tun-mtu", 2, [1400]), ("tun-mtu-extra", 3, [32]),
            ("link-mtu", 4, [1450]), ("mssfix", 5, [1300, "mtu"]),
            ("fragment", 6, [1350, "mtu"]), ("max-packet-size", 9, [1250]),
        ):
            self.assertEqual(server[name], [{"line": line, "arguments": args}])
        self.assertEqual(pushed["tun-mtu"], [{"line": 7, "arguments": [1400]}])
        self.assertEqual(pushed["mssfix"], [{"line": 8, "arguments": [1200, "fixed"]}])

    def test_mssfix_variants_preserve_disabled_and_implicit_values(self):
        for text, args in (("mssfix", []), ("mssfix 0", [0]), ("mssfix 1300", [1300]),
                           ("mssfix 1300 fixed", [1300, "fixed"])):
            with self.subTest(text=text):
                report = self.report(text)
                self.assertTrue(report["complete"])
                self.assertEqual(report["settings"]["server"]["mssfix"][0]["arguments"], args)

    def test_absent_settings_are_not_reported_as_zero_or_defaults(self):
        report = self.report('proto udp\n# tun-mtu 1400\n; mssfix 0\npush "route 10.0.0.0 255.0.0.0"\n')
        self.assertTrue(report["complete"])
        self.assertEqual(report["settings"], {scope: {name: [] for name in DIRECTIVES}
                                             for scope in ("server", "push")})

    def test_repeated_values_are_preserved_per_scope(self):
        report = self.report('tun-mtu 1500\npush "tun-mtu 1400"\ntun-mtu 1300\n')
        self.assertEqual(report["settings"]["server"]["tun-mtu"], [
            {"line": 1, "arguments": [1500]}, {"line": 3, "arguments": [1300]},
        ])
        self.assertEqual(report["warnings"], ["Line 3: repeated server tun-mtu; review all occurrences."])
        self.assertFalse(report["complete"])

    def test_malformed_values_are_omitted_without_echoing_them(self):
        for text in ("tun-mtu", "tun-mtu -1", "tun-mtu 1.5", "tun-mtu ١٤٠٠",
                     "tun-mtu 1400 mtu", "fragment", "fragment 1400 fixed", "mssfix mtu",
                     "mssfix 1400 mtu extra", "mssfix 12345678901", 'mssfix "',
                     'push "mssfix 1400" extra', 'push ""',
                     'mssfix PRIVATE_SENTINEL', 'push "fragment PRIVATE_SENTINEL"'):
            with self.subTest(text=text):
                report = self.report(text)
                self.assertTrue(report["available"])
                self.assertFalse(report["complete"])
                self.assertFalse(any(entries for scope in report["settings"].values() for entries in scope.values()))
                self.assertNotIn("PRIVATE_SENTINEL", json.dumps(report))

    def test_inline_material_and_unrelated_options_never_reach_output(self):
        report = self.report(
            '<key>\nPRIVATE_SENTINEL\nmssfix 999\n</key> # end\n'
            '<tls-crypt>PRIVATE_SENTINEL</tls-crypt>\n'
            '<ca>\ntun-mtu 999\n</ca>\n'
            'key /private/PRIVATE_SENTINEL\nsetenv PRIVATE_SENTINEL PRIVATE_SENTINEL\n'
            'push "echo PRIVATE_SENTINEL"\nmssfix 1300\n'
        )
        self.assertTrue(report["complete"])
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(report))
        self.assertNotIn("999", json.dumps(report))
        self.assertEqual(report["settings"]["server"]["mssfix"][0]["arguments"], [1300])

    def test_unterminated_blocks_fail_closed(self):
        report = self.report('<key>\nPRIVATE_SENTINEL\nmssfix 999\n')
        self.assertFalse(report["complete"])
        self.assertEqual(report["settings"]["server"]["mssfix"], [])
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(report))

    def test_includes_overrides_and_connection_blocks_are_flagged(self):
        report = self.report(
            'config /PRIVATE_SENTINEL\npush-reset\npush-remove PRIVATE_SENTINEL\n'
            'compat-mode 2.4\n<connection>\nmssfix 999\n</connection>\n'
        )
        self.assertFalse(report["complete"])
        self.assertEqual(len(report["warnings"]), 5)
        self.assertEqual(report["settings"]["server"]["mssfix"], [])
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(report))

    def test_missing_file_does_not_create_state(self):
        report = collect_mtu(self.path)
        self.assertFalse(report["available"])
        self.assertFalse(report["complete"])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_unreadable_file_hides_exception_details(self):
        with patch("openvpn_addon.mtu.os.open", side_effect=PermissionError("PRIVATE_SENTINEL")):
            report = collect_mtu(self.path)
        self.assertFalse(report["available"])
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(report))

    def test_invalid_utf8_discards_partial_results(self):
        self.path.write_bytes(b"mssfix 1400\n" + b"# padding\n" * 2000 + b"\xff")
        report = collect_mtu(self.path)
        self.assertFalse(report["available"])
        self.assertEqual(report["settings"]["server"]["mssfix"], [])

    def test_symlinks_and_special_files_are_refused(self):
        target = self.root / "target"
        target.write_text("mssfix 999\n", encoding="utf-8")
        self.path.symlink_to(target)
        self.assertFalse(collect_mtu(self.path)["available"])
        self.path.unlink()
        os.mkfifo(self.path)
        self.assertFalse(collect_mtu(self.path)["available"])
        self.path.unlink()
        self.path.mkdir()
        self.assertFalse(collect_mtu(self.path)["available"])
        directory_link = self.root / "runtime"
        directory_link.symlink_to(self.root, target_is_directory=True)
        self.assertFalse(collect_mtu(directory_link / "target")["available"])

    def test_only_server_config_is_opened_and_no_writes_occur(self):
        self.path.write_text('config missing\nkey missing\nclient-config-dir missing\nmssfix 1300\n', encoding="utf-8")
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns
        with patch("openvpn_addon.mtu.os.open", wraps=os.open) as opened:
            collect_mtu(self.path)
        opened.assert_called_once_with(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), before)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_shipped_template_reports_no_explicit_overrides_without_pki(self):
        templates = Path(__file__).resolve().parents[1] / "addon" / "templates"
        with patch.object(renderer, "TEMPLATES_DIR", templates):
            report = self.report(renderer.render_server_config(AddonConfig()))
        self.assertTrue(report["complete"])
        self.assertFalse(any(entries for scope in report["settings"].values() for entries in scope.values()))

    def test_cli_emits_json_and_exit_codes_without_loading_options_or_pki(self):
        with patch.object(config_module, "DATA_ROOT", self.root), \
             patch.object(main, "load_and_validate", side_effect=AssertionError("options read")), \
             patch.object(main, "PkiManager", side_effect=AssertionError("PKI read")), \
             patch.object(main, "ensure_dirs", side_effect=AssertionError("state mutation")):
            args = main.build_parser().parse_args(["mtu"])
            self.assertIs(args.func, main.cmd_mtu)
            for content, expected in ((None, 2), ("mssfix 0", 0), ("mssfix invalid", 1)):
                with self.subTest(content=content):
                    if content is not None:
                        path = AddonConfig().server_conf_path
                        path.parent.mkdir(exist_ok=True)
                        path.write_text(content, encoding="utf-8")
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        code = args.func(args)
                    self.assertEqual(code, expected)
                    report = json.loads(output.getvalue())
                    self.assertEqual(report["available"], expected != 2)


if __name__ == "__main__":
    unittest.main()
