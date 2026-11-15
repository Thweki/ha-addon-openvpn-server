import contextlib
import io
import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from openvpn_addon import config as config_module, main, renderer
from openvpn_addon.config import AddonConfig


class ProfileBundleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for target, attribute, value in [
            (config_module, "DATA_ROOT", self.root / "data"),
            (config_module, "SHARE_ROOT", self.root / "share"),
            (renderer, "TEMPLATES_DIR", Path(__file__).resolve().parents[1] / "addon/templates"),
        ]:
            patcher = patch.object(target, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.config = AddonConfig(public_endpoint="new.example.com", public_port=443)
        self.output = self.root / "profiles.zip"
        pki = self.config.pki_dir
        (pki / "issued").mkdir(parents=True)
        (pki / "private").mkdir()
        (pki / "ca.crt").write_text("dummy-ca")
        (pki / "ta.key").write_text("dummy-tls")
        for name in ["alice", "bob", "revoked", "expired", "server"]:
            (pki / "issued" / f"{name}.crt").write_text(
                f"-----BEGIN CERTIFICATE-----\n{name}-dummy-cert\n-----END CERTIFICATE-----\n"
            )
            (pki / "private" / f"{name}.key").write_text(f"{name}-dummy-key")
        (pki / "index.txt").write_text(
            "V\t300101000000Z\t\t01\tunknown\t/CN=alice\n"
            "V\t300101000000Z\t\t02\tunknown\t/CN=bob\n"
            "R\t300101000000Z\t260101000000Z\t03\tunknown\t/CN=revoked\n"
            "E\t200101000000Z\t\t04\tunknown\t/CN=expired\n"
        )

    def bundle(self, *names):
        args = main.build_parser().parse_args(
            ["client", "bundle", *names, "--output", str(self.output)]
        )
        with patch.object(main, "load_and_validate", return_value=self.config), \
                patch("openvpn_addon.pki.PkiManager._run") as mutate, \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            result = args.func(args)
        mutate.assert_not_called()
        self.assertEqual(result, 0)
        self.assertNotIn("dummy-key", stdout.getvalue())
        return stdout.getvalue()

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file() and p != self.output}

    def test_bundles_selected_fresh_profiles_without_mutating_existing_state(self):
        self.config.publish_profiles_to_share = True
        self.config.exports_dir.mkdir()
        (self.config.exports_dir / "alice.ovpn").write_text("stale profile")
        before = self.snapshot()
        stdout = self.bundle("bob", "alice")
        self.assertIn(str(self.output), stdout)
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.config.share_exports_dir.exists())
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(archive.namelist(), ["bob.ovpn", "alice.ovpn"])
            for name in ["alice", "bob"]:
                self.assertEqual(archive.read(f"{name}.ovpn").decode(),
                                 renderer.render_client_config(self.config, name))
                self.assertIn(b"remote new.example.com 443", archive.read(f"{name}.ovpn"))
                self.assertEqual(archive.getinfo(f"{name}.ovpn").external_attr >> 16 & 0o777, 0o600)

    def test_archive_is_private_even_with_permissive_umask(self):
        previous = os.umask(0)
        try:
            self.bundle("alice")
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)

    def test_rejects_ineligible_names_before_reading_profile_material(self):
        for name in ["missing", "revoked", "expired", "server", "../alice", "alice\n"]:
            with self.subTest(name=name), \
                    patch.object(renderer, "_read_file") as read_material:
                with self.assertRaises(ValueError):
                    self.bundle("alice", name)
                read_material.assert_not_called()
                self.assertFalse(self.output.exists())

    def test_duplicate_names_are_rejected(self):
        with self.assertRaises(ValueError):
            self.bundle("alice", "alice")
        self.assertFalse(self.output.exists())

    def test_missing_material_leaves_no_archive(self):
        (self.config.pki_dir / "private/bob.key").unlink()
        with self.assertRaises(FileNotFoundError):
            self.bundle("alice", "bob")
        self.assertFalse(self.output.exists())

    def test_unconfigured_endpoint_leaves_no_archive(self):
        self.config.public_endpoint = ""
        with self.assertRaises(ValueError):
            self.bundle("alice")
        self.assertFalse(self.output.exists())

    def test_existing_archive_is_never_overwritten(self):
        self.output.write_bytes(b"existing archive")
        with self.assertRaises(FileExistsError):
            self.bundle("alice")
        self.assertEqual(self.output.read_bytes(), b"existing archive")

    def test_destination_symlink_is_never_followed(self):
        target = self.root / "target"
        target.write_bytes(b"keep")
        self.output.symlink_to(target)
        with self.assertRaises(FileExistsError):
            self.bundle("alice")
        self.assertEqual(target.read_bytes(), b"keep")
        self.assertTrue(self.output.is_symlink())

    def test_write_failure_removes_incomplete_archive(self):
        with patch("zipfile.ZipFile.writestr", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.bundle("alice")
        self.assertFalse(self.output.exists())

    def test_cli_requires_names_and_explicit_destination(self):
        for arguments in [["client", "bundle", "alice"],
                          ["client", "bundle", "--output", str(self.output)]]:
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main.build_parser().parse_args(arguments)
                self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
