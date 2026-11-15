import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openvpn_addon import config as config_module
from openvpn_addon import main
from openvpn_addon.config import AddonConfig


class ProfileExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch.object(config_module, "DATA_ROOT", self.root / "data"))
        self.enterContext(patch.object(config_module, "SHARE_ROOT", self.root / "share"))
        self.config = AddonConfig(public_endpoint="vpn.example.com")
        self.config.exports_dir.mkdir(parents=True)
        self.config.share_exports_dir.mkdir(parents=True)
        # No real credentials or PKI commands are needed to exercise file publication.
        self.profile = "dummy profile content\n"
        self.render = self.enterContext(
            patch.object(main, "render_client_config", return_value=self.profile)
        )

    def destination(self, publish):
        directory = self.config.share_exports_dir if publish else self.config.exports_dir
        return directory / "alice.ovpn"

    def export(self, publish):
        self.config.publish_profiles_to_share = publish
        return main.export_client(self.config, "alice")

    def assert_private_profile(self, path):
        self.assertFalse(path.is_symlink())
        self.assertEqual(self.profile, path.read_text(encoding="utf-8"))
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_export_keeps_share_opt_in(self):
        result = self.export(False)
        self.assertEqual(self.destination(False), result.export_path)
        self.assertIsNone(result.publish_path)
        self.assert_private_profile(result.export_path)
        self.assertEqual([], list(self.config.share_exports_dir.iterdir()))

    def test_complete_private_file_is_published_atomically_under_permissive_umask(self):
        replace = os.replace
        published = []

        def inspect_replace(source, destination):
            source, destination = Path(source), Path(destination)
            self.assertEqual(destination.parent, source.parent)
            self.assert_private_profile(source)
            self.assertEqual("old profile", destination.read_text(encoding="utf-8"))
            published.append(destination)
            return replace(source, destination)

        for publish in (False, True):
            path = self.destination(publish)
            path.write_text("old profile", encoding="utf-8")
            path.chmod(0o644)
        previous_umask = os.umask(0)
        try:
            with patch("os.replace", side_effect=inspect_replace):
                result = self.export(True)
        finally:
            os.umask(previous_umask)
        self.assertEqual([result.export_path, result.publish_path], published)
        for path in published:
            self.assert_private_profile(path)
            self.assertEqual([path], list(path.parent.iterdir()))

    def test_existing_destination_symlinks_are_replaced_without_touching_targets(self):
        for publish in (False, True):
            for dangling in (False, True):
                with self.subTest(publish=publish, dangling=dangling):
                    target = self.root / f"target-{publish}-{dangling}"
                    if not dangling:
                        target.write_text("unrelated file", encoding="utf-8")
                        target.chmod(0o644)
                    path = self.destination(publish)
                    path.unlink(missing_ok=True)
                    path.symlink_to(target)
                    self.export(publish)
                    self.assert_private_profile(path)
                    if dangling:
                        self.assertFalse(target.exists())
                    else:
                        self.assertEqual("unrelated file", target.read_text(encoding="utf-8"))
                        self.assertEqual(0o644, stat.S_IMODE(target.stat().st_mode))

    def test_existing_destination_hardlinks_are_replaced_without_touching_targets(self):
        for publish in (False, True):
            with self.subTest(publish=publish):
                target = self.root / f"hardlink-target-{publish}"
                target.write_text("unrelated file", encoding="utf-8")
                target.chmod(0o644)
                path = self.destination(publish)
                path.unlink(missing_ok=True)
                path.hardlink_to(target)
                self.export(publish)
                self.assert_private_profile(path)
                self.assertEqual("unrelated file", target.read_text(encoding="utf-8"))
                self.assertEqual(0o644, stat.S_IMODE(target.stat().st_mode))

    def test_failed_replacement_preserves_previous_profile_and_removes_temporary_file(self):
        replace = os.replace
        for publish in (False, True):
            with self.subTest(publish=publish):
                path = self.destination(publish)
                path.write_text("old profile", encoding="utf-8")

                def fail_destination(source, destination):
                    if Path(destination) == path:
                        raise OSError("simulated replacement failure")
                    return replace(source, destination)

                with patch("os.replace", side_effect=fail_destination):
                    with self.assertRaisesRegex(OSError, "simulated replacement failure"):
                        self.export(publish)
                self.assertEqual("old profile", path.read_text(encoding="utf-8"))
                self.assertEqual([path], list(path.parent.iterdir()))

    def test_failed_write_preserves_previous_profile_and_removes_temporary_file(self):
        path = self.destination(False)
        path.write_text("old profile", encoding="utf-8")
        self.render.return_value = "invalid UTF-8 surrogate: \ud800"
        with self.assertRaises(UnicodeEncodeError):
            self.export(False)
        self.assertEqual("old profile", path.read_text(encoding="utf-8"))
        self.assertEqual([path], list(path.parent.iterdir()))


if __name__ == "__main__":
    unittest.main()
