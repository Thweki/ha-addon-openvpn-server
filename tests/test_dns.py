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
from openvpn_addon.dns import collect_dns


class DnsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        root_patch = patch.object(config_module, "DATA_ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.config = AddonConfig()
        self.config.runtime_dir.mkdir()

    def write(self, content):
        self.config.server_conf_path.write_text(content, encoding="utf-8")

    def test_real_template_reports_rendered_values_in_order_including_duplicates(self):
        self.config.dns_servers = ["192.168.1.1", "1.1.1.1", "192.168.1.1"]
        self.config.fixed_client_ips = False
        templates = Path(__file__).resolve().parents[1] / "addon" / "templates"
        with patch.object(renderer, "TEMPLATES_DIR", templates):
            rendered = renderer.render_server_config(self.config)
        self.write(rendered)
        # Disk is authoritative even if options have since changed.
        self.config.dns_servers = ["8.8.8.8"]
        report = collect_dns(self.config)
        self.assertTrue(report["complete"])
        self.assertEqual(["192.168.1.1", "1.1.1.1", "192.168.1.1"],
                         [entry["value"] for entry in report["entries"]])
        for entry in report["entries"]:
            self.assertEqual("dhcp-option DNS", entry["option"])
            self.assertEqual(f'push "dhcp-option DNS {entry["value"]}"',
                             rendered.splitlines()[entry["line"] - 1])

    def test_supported_dns_options_and_quoting(self):
        self.write(
            '# push "dhcp-option DNS 9.9.9.9"\n'
            '; push "dhcp-option DNS 9.9.9.9"\n'
            '--push "dhcp-option DNS 2001:db8::1" # comment\n'
            "push 'dhcp-option DNS6 2001:db8::2'\n"
            'push "dhcp-option DOMAIN home.arpa"\n'
            'push "dhcp-option DOMAIN-SEARCH example.com."\n'
            'push "dhcp-option ADAPTER_DOMAIN_SUFFIX home.arpa"\n'
            'push "block-outside-dns"\n'
            'push "register-dns"\n'
            'push "dhcp-option WINS 192.0.2.1"\n'
            'dhcp-option DNS 192.0.2.2\n'
            'push "route 192.0.2.0 255.255.255.0"\n'
        )
        report = collect_dns(self.config)
        self.assertTrue(report["complete"])
        self.assertEqual(list(range(3, 10)), [entry["line"] for entry in report["entries"]])
        self.assertEqual("2001:db8::1", report["entries"][0]["value"])
        self.assertEqual("register-dns", report["entries"][-1]["option"])

    def test_empty_file_means_no_pushes_not_default_dns(self):
        self.write("")
        report = collect_dns(self.config)
        self.assertEqual([], report["entries"])
        self.assertTrue(report["complete"])

    def test_missing_file_is_incomplete_and_does_not_create_state(self):
        self.config.runtime_dir.rmdir()
        report = collect_dns(self.config)
        self.assertFalse(report["complete"])
        self.assertEqual([], report["entries"])
        self.assertEqual([], list(self.root.iterdir()))

    def test_malformed_values_are_omitted_without_echoing_input(self):
        for line in [
            'push "dhcp-option DNS not-an-address"',
            'push "dhcp-option DNS 192.0.2.1#sensitive-data"',
            'push "dhcp-option DNS6 192.0.2.1"',
            'push "dhcp-option DNS fe80::1%private-interface"',
            'push "dhcp-option DOMAIN /private/path"',
            'push "dhcp-option DOMAIN-SEARCH a..b"',
            'push "dhcp-option DOMAIN-ROUTE home.arpa"',
            'push "dhcp-option DNS 192.0.2.1 extra-sensitive-data"',
            'push "block-outside-dns extra-sensitive-data"',
            'push "unterminated-sensitive-data',
            'push',
        ]:
            with self.subTest(line=line):
                self.write(line + '\npush "dhcp-option DNS 1.1.1.1"\n')
                report = collect_dns(self.config)
                self.assertFalse(report["complete"])
                self.assertEqual([{"line": 2, "option": "dhcp-option DNS", "value": "1.1.1.1"}],
                                 report["entries"])
                self.assertEqual(1, report["warnings"][0]["line"])
                self.assertNotIn("sensitive", json.dumps(report))
                self.assertNotIn("private", json.dumps(report))

    def test_credentials_and_inline_blocks_are_never_reported(self):
        self.write(
            'key /private/server.key\nauth-user-pass /private/password\n'
            'push "auth-token sensitive-placeholder"\n'
            '<key>\npush "dhcp-option DNS 9.9.9.9"\n</key>\n'
            '<tls-crypt>\nsensitive-placeholder\n</tls-crypt>\n'
            'push "dhcp-option DNS 1.1.1.1"\n'
        )
        with patch("openvpn_addon.dns.os.open", wraps=os.open) as opened:
            report = collect_dns(self.config)
        self.assertEqual(1, opened.call_count)
        self.assertEqual(self.config.server_conf_path, opened.call_args.args[0])
        self.assertEqual(["1.1.1.1"], [entry["value"] for entry in report["entries"]])
        self.assertNotIn("sensitive-placeholder", json.dumps(report))
        self.assertNotIn("/private", json.dumps(report))

    def test_unterminated_inline_block_is_incomplete(self):
        self.write('<key>\npush "dhcp-option DNS 9.9.9.9"\n')
        report = collect_dns(self.config)
        self.assertFalse(report["complete"])
        self.assertEqual([], report["entries"])

    def test_external_and_dynamic_configuration_is_not_followed(self):
        for directive in ["config", "client-config-dir", "client-connect", "plugin", "push-remove",
                          "push-reset", "dns", "push \"dns server 0 address 1.1.1.1\""]:
            with self.subTest(directive=directive):
                self.write(directive + "\n")
                with patch("openvpn_addon.dns.os.open", wraps=os.open) as opened:
                    report = collect_dns(self.config)
                self.assertEqual(1, opened.call_count)
                self.assertFalse(report["complete"])
                self.assertEqual([], report["entries"])

    def test_symlinked_file_and_runtime_directory_are_not_followed(self):
        target = self.root / "other"
        target.write_text('push "dhcp-option DNS 9.9.9.9"\n', encoding="utf-8")
        self.config.server_conf_path.symlink_to(target)
        self.assertFalse(collect_dns(self.config)["complete"])
        self.config.server_conf_path.unlink()
        self.config.runtime_dir.rmdir()
        self.config.runtime_dir.symlink_to(self.root, target_is_directory=True)
        with patch("openvpn_addon.dns.os.open", side_effect=AssertionError("must not open")):
            self.assertFalse(collect_dns(self.config)["complete"])

    def test_special_file_is_not_read(self):
        os.mkfifo(self.config.server_conf_path)
        report = collect_dns(self.config)
        self.assertFalse(report["complete"])
        self.assertEqual([], report["entries"])

    def test_unreadable_and_invalid_encoding_are_reported_safely(self):
        with patch("openvpn_addon.dns.os.open", side_effect=PermissionError("sensitive-placeholder")):
            report = collect_dns(self.config)
        self.assertFalse(report["complete"])
        self.assertNotIn("sensitive-placeholder", json.dumps(report))
        self.config.server_conf_path.write_bytes(b"\xff\xfe")
        self.assertFalse(collect_dns(self.config)["complete"])

    def test_cli_is_read_only_and_independent_of_options_and_pki(self):
        self.write('push "dhcp-option DNS 192.168.1.1"\n')
        before = self.config.server_conf_path.stat()
        output = io.StringIO()
        with patch.object(main, "load_config", side_effect=AssertionError("options not needed")), \
             patch.object(main, "PkiManager", side_effect=AssertionError("PKI not needed")), \
             patch.object(main, "ensure_dirs", side_effect=AssertionError("no writes")), \
             patch.object(main.subprocess, "Popen", side_effect=AssertionError("no subprocesses")), \
             patch("sys.argv", ["openvpn-addon", "dns"]), contextlib.redirect_stdout(output):
            self.assertEqual(0, main.main())
        report = json.loads(output.getvalue())
        self.assertEqual("server.conf", report["source"])
        self.assertEqual("192.168.1.1", report["entries"][0]["value"])
        self.assertEqual(before.st_mtime_ns, self.config.server_conf_path.stat().st_mtime_ns)
        self.assertEqual([self.config.runtime_dir], list(self.root.iterdir()))


if __name__ == "__main__":
    unittest.main()
