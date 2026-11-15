import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import openvpn_addon.config as config_module
from openvpn_addon import main
from openvpn_addon.config import AddonConfig
from openvpn_addon.network import NetworkResult
from openvpn_addon.pki import PkiManager, STATUS_REVOKED, STATUS_VALID


class DefaultClientStartupTests(unittest.TestCase):
    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        self.patch(config_module, "DATA_ROOT", root)
        self.patch(config_module, "SHARE_ROOT", root / "share")
        self.patch(main, "DATA_ROOT", root)
        self.config = AddonConfig(
            public_endpoint="vpn.example.com",
            publish_profiles_to_share=True,
        )
        self.pki = PkiManager(self.config)
        self.index = self.config.pki_dir / "index.txt"
        self.config.pki_dir.mkdir()
        self.easyrsa = self.patch(self.pki, "_run", side_effect=self.run_easyrsa)
        self.patch(self.pki, "_cert_expiry", return_value=None)
        # Base setup normally refreshes the CRL, even when the default client is revoked.
        self.patch(self.pki, "ensure_base", side_effect=self.pki.refresh_crl)
        self.patch(main, "PkiManager", return_value=self.pki)
        self.patch(main, "load_and_validate", return_value=self.config)
        self.patch(main, "render_server_config", return_value="server config\n")
        self.render_profile = self.patch(main, "render_client_config", return_value="test profile\n")
        self.patch(main, "apply_network", return_value=NetworkResult())
        self.patch(main, "remove_network")
        self.patch(main, "start_panel", return_value=[])
        self.patch(main, "RequestWatcher")
        self.patch(main, "OpenVpnSupervisor")
        self.patch(main.signal, "signal")

    def patch(self, target, attribute, *args, **kwargs):
        patcher = patch.object(target, attribute, *args, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def run_easyrsa(self, command, *args, **kwargs):
        """Model EasyRSA state changes without external commands or real credentials."""
        if command == "gen-crl":
            revoked = self.pki.client_status(self.config.client_name) == STATUS_REVOKED
            self.config.crl_path.write_text("revoked\n" if revoked else "valid\n")
            return
        name = args[0]
        if command == "build-client-full":
            issued = self.config.pki_dir / "issued"
            issued.mkdir(exist_ok=True)
            (issued / f"{name}.crt").write_text("test certificate\n")
        elif command not in {"revoke", "unrevoke"}:
            self.fail(f"Unexpected EasyRSA command: {command}")
        status = "R" if command == "revoke" else "V"
        self.index.write_text(f"{status}\t300101000000Z\t\t01\tunknown\t/CN={name}\n")

    def assert_profile_exported(self):
        self.render_profile.assert_called_with(self.config, self.config.client_name)
        for directory in (self.config.exports_dir, self.config.share_exports_dir):
            self.assertEqual("test profile\n", (directory / f"{self.config.client_name}.ovpn").read_text())

    def check_first_startup(self, command):
        self.assertEqual(0, command(argparse.Namespace()))
        self.assertEqual(STATUS_VALID, self.pki.client_status(self.config.client_name))
        self.assertEqual(["gen-crl", "build-client-full"], [call.args[0] for call in self.easyrsa.call_args_list])
        self.assert_profile_exported()
        # Subsequent startup reuses the valid certificate.
        self.easyrsa.reset_mock()
        self.assertEqual(0, command(argparse.Namespace()))
        self.assertEqual(["gen-crl"], [call.args[0] for call in self.easyrsa.call_args_list])
        self.assert_profile_exported()

    def test_init_provisions_default_client(self):
        self.check_first_startup(main.cmd_init)

    def test_run_provisions_default_client(self):
        self.check_first_startup(main.cmd_run)

    def revoke_default_client(self):
        self.pki.ensure_client(self.config.client_name)
        self.pki.revoke_client(self.config.client_name)
        self.easyrsa.reset_mock()

    def check_revoked_startup(self, command):
        self.revoke_default_client()
        original_index = self.index.read_bytes()
        original_crl = self.config.crl_path.read_bytes()
        for restart in range(2):
            with self.subTest(restart=restart):
                self.assertEqual(0, command(argparse.Namespace()))
                self.assertEqual(STATUS_REVOKED, self.pki.client_status(self.config.client_name))
                self.assertEqual(original_index, self.index.read_bytes())
                self.assertEqual(original_crl, self.config.crl_path.read_bytes())
                self.render_profile.assert_not_called()
                for directory in (self.config.exports_dir, self.config.share_exports_dir):
                    self.assertFalse((directory / f"{self.config.client_name}.ovpn").exists())
        self.assertEqual(["gen-crl", "gen-crl"], [call.args[0] for call in self.easyrsa.call_args_list])

    def test_init_preserves_revocation_and_skips_export(self):
        self.check_revoked_startup(main.cmd_init)

    def test_run_preserves_revocation_and_skips_export(self):
        self.check_revoked_startup(main.cmd_run)

    def test_ensure_client_preserves_revocation_by_default(self):
        self.revoke_default_client()
        result = self.pki.ensure_client(self.config.client_name)
        self.assertEqual("revoked", result.action)
        self.assertEqual(STATUS_REVOKED, self.pki.client_status(self.config.client_name))
        self.easyrsa.assert_not_called()

    def test_explicit_client_issue_can_unrevoke_and_export(self):
        self.revoke_default_client()
        args = main.build_parser().parse_args(["client", "issue"])
        self.assertEqual(0, args.func(args))
        self.assertEqual(STATUS_VALID, self.pki.client_status(self.config.client_name))
        self.assertEqual("valid\n", self.config.crl_path.read_text())
        self.assertEqual(["gen-crl", "unrevoke", "gen-crl"], [call.args[0] for call in self.easyrsa.call_args_list])
        self.assert_profile_exported()


if __name__ == "__main__":
    unittest.main()
