import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openvpn_addon.config import AddonConfig, load_config
from openvpn_addon.main import build_parser


class ConfigBackupTests(unittest.TestCase):
    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.root = Path(tempdir.name)
        self.output = self.root / "backup.json"
        self.config = AddonConfig(
            public_endpoint="vpn.example.com",
            dns_servers=["9.9.9.9", "1.1.1.1"],
            local_networks=["192.168.1.0/24"],
            push_routes=["10.20.0.0/16"],
            nat_enabled=False,
        )

    def run_backup(self, output=None):
        args = build_parser().parse_args(["backup-config", "--output", str(output or self.output)])
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("openvpn_addon.main.load_config", return_value=self.config), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = args.func(args)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_export_round_trips_all_effective_settings(self):
        result, stdout, stderr = self.run_backup()
        self.assertEqual(0, result)
        self.assertEqual("", stderr)
        self.assertIn("Configuration backup created", stdout)
        self.assertNotIn(self.config.public_endpoint, stdout)
        self.assertEqual(self.config, load_config(self.output))
        payload = json.loads(self.output.read_text())
        self.assertEqual("9.9.9.9, 1.1.1.1", payload["dns"])
        self.assertEqual("192.168.1.0/24", payload["lan_networks"])
        self.assertEqual("10.20.0.0/16", payload["extra_routes"])
        self.assertNotIn("dns_servers", payload)
        self.assertNotIn("unknown_options", payload)

    def test_export_uses_only_home_assistant_option_names(self):
        self.run_backup()
        # The top-level options block needs no YAML parser to read its keys.
        addon_yaml = Path(__file__).resolve().parents[1] / "addon" / "config.yaml"
        options = addon_yaml.read_text().split("\noptions:\n", 1)[1].split("\nschema:\n", 1)[0]
        expected = {line.strip().split(":", 1)[0] for line in options.splitlines() if line.strip()}
        self.assertEqual(expected, set(json.loads(self.output.read_text())))

    def test_private_from_creation_even_with_permissive_umask(self):
        old_umask = os.umask(0)
        self.addCleanup(os.umask, old_umask)
        real_link = os.link

        def inspect_link(source, destination):
            self.assertEqual(0o600, stat.S_IMODE(Path(source).stat().st_mode))
            self.assertEqual(self.config, load_config(Path(source)))
            real_link(source, destination)

        with patch("openvpn_addon.backup.os.link", side_effect=inspect_link):
            self.assertEqual(0, self.run_backup()[0])
        self.assertEqual(0o600, stat.S_IMODE(self.output.stat().st_mode))
        self.assertEqual([self.output], list(self.root.iterdir()))

    def test_existing_file_is_preserved(self):
        self.output.write_text("existing backup")
        result, _, stderr = self.run_backup()
        self.assertEqual(1, result)
        self.assertIn("already exists", stderr)
        self.assertEqual("existing backup", self.output.read_text())
        self.assertEqual([self.output], list(self.root.iterdir()))

    def test_destination_symlinks_are_not_followed(self):
        for exists in (False, True):
            with self.subTest(target_exists=exists):
                target = self.root / "target"
                if exists:
                    target.write_text("untouched")
                self.output.symlink_to(target)
                self.assertEqual(1, self.run_backup()[0])
                self.assertTrue(self.output.is_symlink())
                self.assertEqual(exists, target.exists())
                if exists:
                    self.assertEqual("untouched", target.read_text())
                self.output.unlink()

    def test_existing_hardlink_is_preserved(self):
        target = self.root / "target"
        target.write_text("untouched")
        os.link(target, self.output)
        self.assertEqual(1, self.run_backup()[0])
        self.assertEqual("untouched", target.read_text())

    def test_failed_publish_removes_temporary_file_and_hides_error_details(self):
        with patch("openvpn_addon.backup.os.link", side_effect=OSError("sensitive-error-marker")):
            result, stdout, stderr = self.run_backup()
        self.assertEqual(1, result)
        self.assertNotIn("sensitive-error-marker", stdout + stderr)
        self.assertEqual([], list(self.root.iterdir()))

    def test_failed_write_leaves_no_partial_backup(self):
        with patch("openvpn_addon.backup.json.dump", side_effect=OSError("disk full")):
            self.assertEqual(1, self.run_backup()[0])
        self.assertEqual([], list(self.root.iterdir()))

    def test_invalid_configuration_is_not_written_or_logged(self):
        self.config.protocol = "sensitive-invalid-marker"
        result, stdout, stderr = self.run_backup()
        self.assertEqual(1, result)
        self.assertNotIn(self.config.protocol, stdout + stderr)
        self.assertFalse(self.output.exists())

    def test_unknown_options_and_pki_are_not_exported(self):
        options = self.root / "options.json"
        options.write_text(json.dumps({"private_key": "synthetic-key-marker", "private-name-marker": 1}))
        self.config = load_config(options)
        with patch("openvpn_addon.main.PkiManager", side_effect=AssertionError("PKI accessed")):
            result, stdout, stderr = self.run_backup()
        self.assertEqual(0, result)
        combined = self.output.read_text() + stdout + stderr
        for marker in ("private_key", "synthetic-key-marker", "private-name-marker"):
            self.assertNotIn(marker, combined)

    def test_missing_destination_parent_is_not_created(self):
        result, _, _ = self.run_backup(self.root / "missing" / "backup.json")
        self.assertEqual(1, result)
        self.assertFalse((self.root / "missing").exists())

    def test_output_argument_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(["backup-config"])
        self.assertEqual(2, raised.exception.code)

    def test_options_load_error_does_not_print_option_values(self):
        args = build_parser().parse_args(["backup-config", "--output", str(self.output)])
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("openvpn_addon.main.load_config", side_effect=ValueError("sensitive-value-marker")), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(1, args.func(args))
        self.assertNotIn("sensitive-value-marker", stdout.getvalue() + stderr.getvalue())
        self.assertFalse(self.output.exists())

    def test_destination_created_during_export_is_preserved(self):
        real_link = os.link

        def competing_writer(source, destination):
            Path(destination).write_text("competing backup")
            real_link(source, destination)

        with patch("openvpn_addon.backup.os.link", side_effect=competing_writer):
            self.assertEqual(1, self.run_backup()[0])
        self.assertEqual("competing backup", self.output.read_text())
        self.assertEqual([self.output], list(self.root.iterdir()))


if __name__ == "__main__":
    unittest.main()
