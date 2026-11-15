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

from openvpn_addon import config as config_module, renderer
from openvpn_addon.config import AddonConfig
from openvpn_addon.main import build_parser
from openvpn_addon.routes import collect_routes


class RoutesTests(unittest.TestCase):
    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.root = Path(tempdir.name)
        patcher = patch.object(config_module, "DATA_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = AddonConfig()
        self.config.ccd_dir.mkdir(parents=True)

    def server(self, content, ccd=False):
        if ccd:
            content += f'client-config-dir "{self.config.ccd_dir}"\n'
        self.config.server_conf_path.write_text(content, encoding="utf-8")

    def client(self, name, content):
        (self.config.ccd_dir / name).write_text(content, encoding="utf-8")

    def test_rendered_routes_are_normalized_deduplicated_and_not_pending_options(self):
        self.config.local_networks = ["192.168.1.9/24"]
        self.config.push_routes = ["192.168.1.0/24", "10.20.0.0/16"]
        self.config.redirect_gateway = True
        templates = Path(__file__).resolve().parents[1] / "addon" / "templates"
        with patch.object(renderer, "TEMPLATES_DIR", templates):
            self.server(renderer.render_server_config(self.config))
        self.config.local_networks = ["172.16.0.0/16"]
        report = collect_routes(self.config)
        self.assertTrue(report["complete"])
        self.assertTrue(report["ccd_enabled"])
        routes = [entry["network"] for entry in report["entries"] if "network" in entry]
        self.assertEqual(routes, ["192.168.1.0/24", "10.20.0.0/16"])
        self.assertEqual(report["entries"][0]["flags"], ["def1", "bypass-dhcp"])

    def test_ccd_routes_keep_owner_line_and_order(self):
        self.server('push "route 192.168.1.0 255.255.255.0"\n', ccd=True)
        self.client("zebra", "iroute 10.20.30.0 255.255.255.0\n")
        self.client("alice", '# comment\npush-reset\npush "route 10.2.0.0 255.255.0.0 vpn_gateway 5"\niroute 10.1.2.3\n')
        self.client("DEFAULT", "iroute 172.16.0.0 255.255.0.0\n")
        entries = collect_routes(self.config)["entries"]
        self.assertEqual([entry["source"] for entry in entries],
                         ["server.conf", "ccd/DEFAULT", "ccd/alice", "ccd/alice", "ccd/alice", "ccd/zebra"])
        self.assertEqual(entries[2], {"source": "ccd/alice", "line": 2, "directive": "push-reset"})
        self.assertEqual(entries[3]["gateway"], "vpn_gateway")
        self.assertEqual(entries[3]["metric"], 5)
        self.assertEqual(entries[4]["network"], "10.1.2.3/32")

    def test_ipv6_and_host_routes(self):
        self.server('--push "route 192.0.2.1" ; comment\npush "route-ipv6 2001:db8:1::9/64 2001:db8::1 10"\n', ccd=True)
        self.client("alice", "iroute-ipv6 2001:db8:2::/64\n")
        report = collect_routes(self.config)
        self.assertTrue(report["complete"])
        self.assertEqual([e["network"] for e in report["entries"]],
                         ["192.0.2.1/32", "2001:db8:1::/64", "2001:db8:2::/64"])

    def test_disabled_ccd_does_not_report_stale_iroutes(self):
        self.server("")
        self.client("alice", "iroute 10.20.30.0 255.255.255.0\n")
        report = collect_routes(self.config)
        self.assertTrue(report["complete"])
        self.assertFalse(report["ccd_enabled"])
        self.assertEqual(report["entries"], [])

    def test_runtime_ccd_enablement_wins_over_pending_options(self):
        self.server("", ccd=True)
        self.client("alice", "iroute 10.1.2.3\n")
        self.config.fixed_client_ips = False
        self.config.duplicate_cn = True
        self.assertEqual(len(collect_routes(self.config)["entries"]), 1)

    def test_missing_files_are_not_confused_with_empty_routes(self):
        report = collect_routes(self.config)
        self.assertFalse(report["complete"])
        self.assertEqual(report["entries"], [])
        self.server("", ccd=True)
        self.config.ccd_dir.rmdir()
        self.assertFalse(collect_routes(self.config)["complete"])

    def test_bad_lines_are_redacted_and_valid_routes_survive(self):
        self.server('push "route FAKE_SENSITIVE_VALUE"\npush "route 192.0.2.1\npush "route 192.0.2.2"\n')
        report = collect_routes(self.config)
        self.assertFalse(report["complete"])
        self.assertEqual([w["line"] for w in report["warnings"]], [1, 2])
        self.assertNotIn("FAKE_SENSITIVE_VALUE", json.dumps(report))
        self.assertEqual(report["entries"][0]["network"], "192.0.2.2/32")

    def test_unsupported_operands_are_not_echoed(self):
        for route in ["route 192.0.2.0 0.0.0.255", "route 192.0.2.0 255.255.255.0 FAKE_VALUE",
                      "route 192.0.2.0 255.255.255.0 vpn_gateway FAKE_VALUE",
                      "route-ipv6 FAKE_VALUE", "redirect-gateway FAKE_VALUE"]:
            with self.subTest(route=route):
                self.server(f'push "{route}"\n')
                report = collect_routes(self.config)
                self.assertFalse(report["complete"])
                self.assertEqual(report["entries"], [])
                self.assertNotIn("FAKE_VALUE", json.dumps(report))

    def test_credentials_inline_blocks_and_non_route_pushes_are_omitted(self):
        self.server('key /private/FAKE_VALUE\npush "auth-token FAKE_VALUE"\n'
                    '<key>\nFAKE_VALUE\npush "route 192.0.2.3"\n</key>\n'
                    'push "route 192.0.2.4"\n')
        report = collect_routes(self.config)
        self.assertTrue(report["complete"])
        self.assertNotIn("FAKE_VALUE", json.dumps(report))
        self.assertEqual([e["network"] for e in report["entries"]], ["192.0.2.4/32"])

    def test_dynamic_includes_and_filters_warn_without_reading_or_executing(self):
        self.server('config /private/FAKE_VALUE\nclient-connect /FAKE_VALUE\nplugin /FAKE_VALUE\n', ccd=True)
        self.client("alice", 'push-remove "FAKE_VALUE"\n')
        report = collect_routes(self.config)
        self.assertFalse(report["complete"])
        self.assertEqual(len(report["warnings"]), 4)
        self.assertNotIn("FAKE_VALUE", json.dumps(report))

    def test_external_ccd_directory_is_not_read(self):
        self.server(f'client-config-dir "{self.root}"\n')
        (self.root / "alice").write_text("iroute 192.0.2.3\n")
        report = collect_routes(self.config)
        self.assertFalse(report["complete"])
        self.assertEqual(report["entries"], [])

    def test_symlinks_and_special_files_are_not_read(self):
        self.server("", ccd=True)
        target = self.root / "private.key"
        target.write_text("iroute 192.0.2.3\n")
        (self.config.ccd_dir / "alice").symlink_to(target)
        os.mkfifo(self.config.ccd_dir / "bob")
        (self.config.ccd_dir / "directory").mkdir()
        report = collect_routes(self.config)
        self.assertFalse(report["complete"])
        self.assertEqual(report["entries"], [])
        self.assertEqual(len(report["warnings"]), 3)
        self.config.server_conf_path.unlink()
        self.config.server_conf_path.symlink_to(target)
        self.assertFalse(collect_routes(self.config)["complete"])

    def test_symlinked_ccd_directory_is_not_read(self):
        self.server("", ccd=True)
        self.config.ccd_dir.rmdir()
        self.config.ccd_dir.symlink_to(self.root)
        report = collect_routes(self.config)
        self.assertFalse(report["complete"])
        self.assertEqual(report["entries"], [])

    def test_non_utf8_and_unclosed_inline_blocks_warn(self):
        self.config.server_conf_path.write_bytes(b"\xff\xfe")
        self.assertFalse(collect_routes(self.config)["complete"])
        self.server("<key>\n")
        self.assertFalse(collect_routes(self.config)["complete"])

    def test_cli_is_read_only_and_does_not_load_options_or_pki(self):
        self.server('push "route 192.0.2.1"\n', ccd=True)
        self.client("alice", "iroute 10.1.2.3\n")
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        with patch("openvpn_addon.main.load_and_validate", side_effect=AssertionError), \
             patch("openvpn_addon.main.PkiManager", side_effect=AssertionError), \
             patch("subprocess.run", side_effect=AssertionError):
            args = build_parser().parse_args(["routes"])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(args.func(args), 0)
            self.assertEqual(json.loads(output.getvalue())["source"], "runtime_files")
        after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_cli_module_returns_json_and_failure_for_missing_runtime(self):
        env = {**os.environ, "OPENVPN_ADDON_DATA": str(self.root / "missing"),
               "OPENVPN_ADDON_OPTIONS": str(self.root / "invalid.json")}
        (self.root / "invalid.json").write_text("invalid JSON")
        result = subprocess.run([sys.executable, "-m", "openvpn_addon.main", "routes"],
                                env=env, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)["complete"])
        self.assertEqual(result.stderr, "")
        self.assertFalse((self.root / "missing").exists())


if __name__ == "__main__":
    unittest.main()
