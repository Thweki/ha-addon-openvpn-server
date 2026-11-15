import contextlib
import io
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openvpn_addon.config import load_config
from openvpn_addon.config_import import MAX_SNAPSHOT_BYTES
from openvpn_addon.main import build_parser


# The complete, flat options format emitted by backup-config in PR #20.
SNAPSHOT = {
    "server_host": "0.0.0.0", "public_endpoint": "vpn.example.com",
    "listen_port": 1194, "public_port": 443, "protocol": "udp", "device": "tun",
    "vpn_subnet": "10.8.0.0", "vpn_netmask": "255.255.255.0",
    "dns": "1.1.1.1, 8.8.8.8", "extra_routes": "10.20.0.0/16",
    "lan_networks": "192.168.1.0/24", "nat_enabled": True,
    "redirect_gateway": False, "cipher": "AES-256-GCM", "auth": "SHA256",
    "tls_crypt": True, "client_to_client": False, "duplicate_cn": False,
    "fixed_client_ips": True, "publish_profiles_to_share": False,
    "crl_valid_days": 3650, "inactive_timeout": 0, "keepalive": "10 120",
    "log_level": "info", "status_interval": 30, "client_name": "homeassistant",
    "client_expire_days": 825,
}


class ConfigImportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "snapshot.json"
        self.output = self.root / "options.json"
        self.write_snapshot(SNAPSHOT)

    def write_snapshot(self, payload):
        self.source.write_text(json.dumps(payload), encoding="utf-8")

    def run_command(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            args = build_parser().parse_args(["import-config", str(self.source), *arguments])
            code = args.func(args)
        return code, stdout.getvalue() + stderr.getvalue()

    def test_import_preserves_snapshot_and_loads_as_options(self):
        original = self.source.read_bytes()
        code, message = self.run_command("--output", str(self.output))
        self.assertEqual(0, code, message)
        self.assertIn("staged", message)
        self.assertEqual(SNAPSHOT, json.loads(self.output.read_text()))
        self.assertEqual(original, self.source.read_bytes())
        self.assertEqual(0o600, stat.S_IMODE(self.output.stat().st_mode))
        config = load_config(self.output)
        self.assertEqual(["192.168.1.0/24"], config.local_networks)
        self.assertEqual(["10.20.0.0/16"], config.push_routes)
        self.assertEqual(["1.1.1.1", "8.8.8.8"], config.dns_servers)
        self.assertEqual(443, config.public_port)
        self.assertEqual([], config.unknown_options)
        self.assertEqual({self.source, self.output}, set(self.root.iterdir()))

    def test_check_does_not_read_live_options_or_access_pki(self):
        with patch("openvpn_addon.main.load_config", side_effect=AssertionError), patch(
            "openvpn_addon.main.PkiManager", side_effect=AssertionError
        ):
            code, message = self.run_command("--check")
        self.assertEqual(0, code, message)
        self.assertEqual({self.source}, set(self.root.iterdir()))

    def test_requires_one_explicit_mode(self):
        for arguments in ([], ["--check", "--output", str(self.output)]):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    build_parser().parse_args(["import-config", str(self.source), *arguments])
                self.assertEqual(2, error.exception.code)

    def test_refuses_unknown_and_pki_keys(self):
        for key in ("pki", "private_key", "ca", "certificates", "unknown_options", "future_option"):
            with self.subTest(key=key):
                self.write_snapshot({**SNAPSHOT, key: "sensitive-input-marker"})
                self.assert_rejected()

    def test_refuses_missing_keys_and_legacy_aliases(self):
        for key in SNAPSHOT:
            with self.subTest(key=key):
                payload = dict(SNAPSHOT)
                payload.pop(key)
                self.write_snapshot(payload)
                self.assert_rejected()
        payload = dict(SNAPSHOT)
        payload["dns_servers"] = payload.pop("dns")
        self.write_snapshot(payload)
        self.assert_rejected()

    def test_refuses_wrong_types_without_coercion(self):
        for key, value in (
            ("public_port", True), ("public_port", 443.0), ("public_port", "443"),
            ("nat_enabled", "false"), ("nat_enabled", 0), ("dns", ["1.1.1.1"]),
            ("cipher", {"key": "sensitive-input-marker"}), ("server_host", None),
        ):
            with self.subTest(key=key, value=value):
                self.write_snapshot({**SNAPSHOT, key: value})
                self.assert_rejected()

    def test_refuses_invalid_config_and_directive_injection(self):
        for key, value in (
            ("public_port", 0), ("protocol", "sensitive-input-marker"),
            ("dns", "invalid"), ("lan_networks", "10.8.0.0/24"),
            ("vpn_netmask", "255.0.255.0"), ("client_name", "../client"),
            ("status_interval", -1), ("device", "tun\nscript-security 2"),
            ("cipher", "AES-256-GCM config /tmp/config"), ("auth", "SHA256\rverb 9"),
            ("keepalive", "10 120\nup /tmp/script"), ("keepalive", "0 120"),
            ("public_endpoint", "vpn.example.com config"),
            ("public_endpoint", "vpn.example.com\x00"),
            ("public_endpoint", "-----BEGIN PRIVATE KEY-----"),
            ("public_endpoint", "<key>sensitive-input-marker</key>"),
        ):
            with self.subTest(key=key):
                self.write_snapshot({**SNAPSHOT, key: value})
                self.assert_rejected()

    def test_refuses_duplicate_keys(self):
        original = self.source.read_text()
        self.source.write_text(original[:-1] + ', "public_port": 1194}')
        self.assert_rejected()

    def test_refuses_malformed_nonobject_and_oversized_input(self):
        for raw in (b"{", b"[]", b"null", b"\xff", b" " * (MAX_SNAPSHOT_BYTES + 1),
                    b"[" * 2000 + b"]" * 2000):
            with self.subTest(length=len(raw)):
                self.source.write_bytes(raw)
                self.assert_rejected()

    def test_missing_source_fails_without_output(self):
        self.source.unlink()
        code, message = self.run_command("--output", str(self.output))
        self.assertEqual(1, code)
        self.assertNotIn(str(self.source), message)
        self.assertFalse(self.output.exists())

    def test_refuses_overwriting_existing_file_or_source(self):
        self.output.write_text("original")
        code, _ = self.run_command("--output", str(self.output))
        self.assertEqual(1, code)
        self.assertEqual("original", self.output.read_text())
        original = self.source.read_bytes()
        code, _ = self.run_command("--output", str(self.source))
        self.assertEqual(1, code)
        self.assertEqual(original, self.source.read_bytes())
        self.assertEqual({self.source, self.output}, set(self.root.iterdir()))

    def test_refuses_destination_symlink_including_dangling(self):
        for target in (self.source, self.root / "missing"):
            with self.subTest(target=target.name):
                self.output.symlink_to(target)
                code, _ = self.run_command("--output", str(self.output))
                self.assertEqual(1, code)
                self.assertTrue(self.output.is_symlink())
                self.output.unlink()
        self.assertFalse((self.root / "missing").exists())
        self.assertEqual(SNAPSHOT, json.loads(self.source.read_text()))

    def test_failed_write_leaves_no_output_or_temporary_file(self):
        for operation in ("json.dump", "os.fsync", "os.link"):
            with self.subTest(operation=operation), patch(
                "openvpn_addon.config_import." + operation,
                side_effect=OSError("sensitive-input-marker"),
            ):
                code, message = self.run_command("--output", str(self.output))
                self.assertEqual(1, code)
                self.assertNotIn("sensitive-input-marker", message)
                self.assertEqual({self.source}, set(self.root.iterdir()))

    def test_destination_created_during_import_is_preserved(self):
        import os
        link = os.link

        def race(source, destination):
            destination.write_text("concurrent writer")
            link(source, destination)

        with patch("openvpn_addon.config_import.os.link", side_effect=race):
            code, _ = self.run_command("--output", str(self.output))
        self.assertEqual(1, code)
        self.assertEqual("concurrent writer", self.output.read_text())
        self.assertEqual({self.source, self.output}, set(self.root.iterdir()))

    def test_missing_output_directory_is_not_created(self):
        destination = self.root / "absent" / "options.json"
        code, _ = self.run_command("--output", str(destination))
        self.assertEqual(1, code)
        self.assertFalse(destination.parent.exists())

    def test_success_does_not_log_option_values_or_paths(self):
        self.write_snapshot({**SNAPSHOT, "public_endpoint": "sensitive-input-marker.example"})
        code, message = self.run_command("--output", str(self.output))
        self.assertEqual(0, code)
        self.assertNotIn("sensitive-input-marker", message)
        self.assertNotIn(str(self.root), message)

    def assert_rejected(self):
        code, message = self.run_command("--output", str(self.output))
        self.assertEqual(1, code)
        self.assertNotIn("sensitive-input-marker", message)
        self.assertNotIn(str(self.root), message)
        self.assertFalse(self.output.exists())
        self.assertEqual({self.source}, set(self.root.iterdir()))


if __name__ == "__main__":
    unittest.main()
