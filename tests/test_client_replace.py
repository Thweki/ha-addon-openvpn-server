import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openvpn_addon import config as config_module, main, renderer
from openvpn_addon.config import AddonConfig
from openvpn_addon.pki import PkiManager


class ClientReplacementTests(unittest.TestCase):
    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        for module, name, value in [
            (config_module, "DATA_ROOT", root / "data"),
            (config_module, "SHARE_ROOT", root / "share"),
            (main, "DATA_ROOT", root / "data"),
            (renderer, "TEMPLATES_DIR", Path(__file__).resolve().parents[1] / "addon/templates"),
        ]:
            patcher = patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.config = AddonConfig(public_endpoint="vpn.example.com", publish_profiles_to_share=True)
        main.ensure_dirs(self.config)
        for directory in ("issued", "private", "reqs"):
            (self.config.pki_dir / directory).mkdir(parents=True)
        (self.config.pki_dir / "ca.crt").write_text("test CA")
        self.config.tls_crypt_path.write_text("test TLS key")
        self.states = {"alice": "V"}
        self.write_index()
        self.write_identity("alice")
        for directory in (self.config.exports_dir, self.config.share_exports_dir):
            (directory / "alice.ovpn").write_text("old profile")
        self.pki = PkiManager(self.config)
        self.calls = []
        patcher = patch.object(self.pki, "_run", side_effect=self.run_easyrsa)
        self.command = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(main, "sync_client_ips")
        self.sync = patcher.start()
        self.addCleanup(patcher.stop)

    def write_index(self):
        (self.config.pki_dir / "index.txt").write_text("".join(
            f"{state}\t200101000000Z\t\t{serial:02X}\tunknown\t/CN={name}\n"
            for serial, (name, state) in enumerate(self.states.items(), 1)
        ))

    def write_identity(self, name):
        (self.config.pki_dir / "issued" / f"{name}.crt").write_text(
            f"-----BEGIN CERTIFICATE-----\ntest-{name}\n-----END CERTIFICATE-----\n"
        )
        (self.config.pki_dir / "private" / f"{name}.key").write_text(f"test-key-{name}")

    def run_easyrsa(self, *args, **kwargs):
        self.calls.append(args)
        if args[0] == "build-client-full":
            self.states[args[1]] = "V"
            self.write_identity(args[1])
            self.write_index()
        elif args[0] == "revoke":
            # Every enabled export must be complete before retiring the old credential.
            directories = [self.config.exports_dir]
            if self.config.publish_profiles_to_share:
                directories.append(self.config.share_exports_dir)
            for directory in directories:
                self.assertIn("test-alice-next", (directory / "alice-next.ovpn").read_text())
            self.states[args[1]] = "R"
            self.write_index()

    def replace(self, source="alice", target="alice-next"):
        return main.replace_client(self.config, self.pki, source, target)

    def assert_old_exports_remain(self):
        for directory in (self.config.exports_dir, self.config.share_exports_dir):
            self.assertEqual("old profile", (directory / "alice.ovpn").read_text())

    def test_replaces_expired_certificate_with_fresh_identity_and_exports(self):
        # The V entry has an expired date, as in an index not yet updated by EasyRSA.
        result = self.replace()
        self.assertEqual([("build-client-full", "alice-next", "nopass"),
                          ("revoke", "alice"), ("gen-crl",)], self.calls)
        self.assertEqual(str(self.config.client_expire_days), self.command.call_args_list[0].kwargs["days"])
        self.assertEqual("revoked", self.pki.client_status("alice"))
        self.assertEqual("valid", self.pki.client_status("alice-next"))
        self.assertIn("test-key-alice-next", result.export_path.read_text())
        self.assertNotIn("test-key-alice\n", result.export_path.read_text())
        self.assertEqual(result.export_path.read_text(), result.publish_path.read_text())
        self.assertEqual(0o600, result.export_path.stat().st_mode & 0o777)
        self.assertEqual(0o600, result.publish_path.stat().st_mode & 0o777)
        for directory in (self.config.exports_dir, self.config.share_exports_dir):
            self.assertFalse((directory / "alice.ovpn").exists())
        self.sync.assert_called_once_with(self.config, self.pki)

    def test_replacement_without_share_publishing_still_removes_old_share_copy(self):
        self.config.publish_profiles_to_share = False
        result = self.replace()
        self.assertIsNone(result.publish_path)
        self.assertTrue(result.export_path.exists())
        self.assertFalse((self.config.share_exports_dir / "alice-next.ovpn").exists())
        self.assertFalse((self.config.share_exports_dir / "alice.ovpn").exists())

    def test_replaces_index_marked_expired_without_unrevoking_it(self):
        self.states["alice"] = "E"
        self.write_index()
        self.replace()
        self.assertIn(("revoke", "alice"), self.calls)
        self.assertNotIn(("unrevoke", "alice"), self.calls)

    def test_already_revoked_source_stays_revoked_and_crl_is_refreshed(self):
        self.states["alice"] = "R"
        self.write_index()
        self.replace()
        self.assertEqual([("build-client-full", "alice-next", "nopass"), ("gen-crl",)], self.calls)
        self.assertEqual("revoked", self.pki.client_status("alice"))

    def test_invalid_reserved_identical_and_default_names_fail_before_mutation(self):
        for source, target in [("../alice", "next"), ("alice", "../next"),
                               ("server", "next"), ("alice", "server"),
                               ("alice", "alice"), (self.config.client_name, "next")]:
            with self.subTest(source=source, target=target), self.assertRaises(ValueError):
                self.replace(source, target)
        self.command.assert_not_called()

    def test_unknown_source_fails_before_mutation(self):
        with self.assertRaises(ValueError):
            self.replace("unknown")
        self.command.assert_not_called()

    def test_existing_target_is_never_reused_or_unrevoked(self):
        for state in ("V", "E", "R"):
            self.states["alice-next"] = state
            self.write_index()
            with self.subTest(state=state), self.assertRaises(ValueError):
                self.replace()
        self.command.assert_not_called()

    def test_orphaned_target_artifacts_are_not_overwritten(self):
        paths = [self.config.pki_dir / "issued/alice-next.crt",
                 self.config.pki_dir / "private/alice-next.key",
                 self.config.pki_dir / "reqs/alice-next.req",
                 self.config.exports_dir / "alice-next.ovpn",
                 self.config.share_exports_dir / "alice-next.ovpn"]
        for path in paths:
            with self.subTest(path=path):
                path.write_text("existing artifact")
                with self.assertRaises(ValueError):
                    self.replace()
                self.assertEqual("existing artifact", path.read_text())
                path.unlink()
        self.command.assert_not_called()

    def test_dangling_target_symlink_is_not_overwritten(self):
        (self.config.exports_dir / "alice-next.ovpn").symlink_to("missing")
        with self.assertRaises(ValueError):
            self.replace()
        self.command.assert_not_called()

    def test_missing_endpoint_fails_before_issuance(self):
        self.config.public_endpoint = ""
        with self.assertRaises(ValueError):
            self.replace()
        self.command.assert_not_called()

    def test_issuance_failure_keeps_old_credential_and_exports(self):
        self.command.side_effect = subprocess.CalledProcessError(1, "easyrsa")
        with self.assertRaises(subprocess.CalledProcessError):
            self.replace()
        self.assertEqual("valid", self.pki.client_status("alice"))
        self.assert_old_exports_remain()
        self.sync.assert_not_called()

    def test_export_failure_does_not_revoke_old_credential(self):
        with patch.object(main, "publish_export", side_effect=OSError("share unavailable")):
            with self.assertRaises(OSError):
                self.replace()
        self.assertEqual([("build-client-full", "alice-next", "nopass")], self.calls)
        self.assertEqual("valid", self.pki.client_status("alice"))
        self.assert_old_exports_remain()
        self.sync.assert_not_called()

    def test_revocation_or_crl_failure_keeps_exports_for_recovery(self):
        for failed_command in ("revoke", "gen-crl"):
            with self.subTest(command=failed_command):
                def fail(*args, **kwargs):
                    if args[0] == failed_command:
                        raise subprocess.CalledProcessError(1, "easyrsa")
                    self.run_easyrsa(*args, **kwargs)
                self.command.side_effect = fail
                with self.assertRaises(subprocess.CalledProcessError):
                    self.replace()
                self.assert_old_exports_remain()
                self.assertTrue((self.config.exports_dir / "alice-next.ovpn").exists())
                self.sync.assert_not_called()
                # Reset only the fake replacement to exercise the next failure point.
                self.states = {"alice": "V"}
                self.write_index()
                for path in (self.config.pki_dir / "issued/alice-next.crt",
                             self.config.pki_dir / "private/alice-next.key",
                             self.config.exports_dir / "alice-next.ovpn",
                             self.config.share_exports_dir / "alice-next.ovpn"):
                    path.unlink()

    def test_cli_dispatches_replacement_and_reports_profile_path(self):
        args = main.build_parser().parse_args(["client", "replace", "alice", "alice-next"])
        output = io.StringIO()
        with patch.object(main, "load_and_validate", return_value=self.config), \
                patch.object(main, "PkiManager", return_value=self.pki), \
                contextlib.redirect_stdout(output):
            self.assertEqual(0, args.func(args))
        self.assertIn("alice-next.ovpn", output.getvalue())
        self.assertIn("Restart OpenVPN", output.getvalue())
        self.assertNotIn("test-key", output.getvalue())


if __name__ == "__main__":
    unittest.main()
