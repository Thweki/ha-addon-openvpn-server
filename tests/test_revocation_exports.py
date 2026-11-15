import argparse
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from openvpn_addon import config as config_module, main
from openvpn_addon.config import AddonConfig
from openvpn_addon.pki import PkiManager


class RevocationExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.enterContext(patch.object(config_module, "DATA_ROOT", root / "data"))
        self.enterContext(patch.object(config_module, "SHARE_ROOT", root / "share"))
        self.config = AddonConfig()
        self.pki = Mock(spec=PkiManager)
        self.supervisor = Mock(spec=main.OpenVpnSupervisor)
        self.supervisor.is_running.return_value = True
        self.supervisor.restart.return_value = {"status": "ok", "message": "restarted"}
        self.sync = self.enterContext(patch.object(main, "sync_client_ips"))
        self.enterContext(patch.object(main, "load_and_validate", return_value=self.config))
        self.enterContext(patch.object(main, "PkiManager", return_value=self.pki))
        self.profiles = [
            self.config.exports_dir / "alice.ovpn",
            self.config.share_exports_dir / "alice.ovpn",
        ]
        self.other_profiles = [path.with_name("bob.ovpn") for path in self.profiles]
        self._write_profiles()
        for path in self.other_profiles:
            path.write_text("other client fixture", encoding="utf-8")

    def _write_profiles(self):
        for path in self.profiles:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("revoked client fixture", encoding="utf-8")

    def _revoke(self, entrypoint):
        if entrypoint == "cli":
            return main.cmd_client_revoke(argparse.Namespace(name="alice"))
        if entrypoint == "request":
            return main.process_client_request(self.config, self.pki, "revoke", "alice")
        # Exercise the real panel callback without opening a socket.
        with patch.object(main, "panel_bind_addresses", return_value=["127.0.0.1"]), \
                patch.object(main, "PanelServer") as server:
            server.return_value.address = ("127.0.0.1", main.PANEL_PORT)
            main.start_panel(self.config, self.pki, self.supervisor, threading.Event())
            actions = server.call_args.args[1]
        return actions.revoke_client("alice")

    def test_all_entrypoints_remove_only_the_revoked_clients_exports(self):
        for entrypoint in ("cli", "panel", "request"):
            for publish_enabled in (True, False):
                with self.subTest(entrypoint=entrypoint, publish_enabled=publish_enabled):
                    self.config.publish_profiles_to_share = publish_enabled
                    self._write_profiles()
                    self.pki.reset_mock()
                    self.sync.reset_mock()
                    self._revoke(entrypoint)
                    self.pki.revoke_client.assert_called_once_with("alice")
                    self.sync.assert_called_once_with(self.config, self.pki)
                    for path in self.profiles:
                        self.assertFalse(path.exists(), path)
                    for path in self.other_profiles:
                        self.assertEqual("other client fixture", path.read_text(encoding="utf-8"))

    def test_missing_exports_do_not_prevent_revocation(self):
        for entrypoint in ("cli", "panel", "request"):
            for missing in (self.profiles[:1], self.profiles[1:], self.profiles):
                with self.subTest(entrypoint=entrypoint, missing=missing):
                    self._write_profiles()
                    for path in missing:
                        path.unlink()
                    self._revoke(entrypoint)
                    self.assertFalse(any(path.exists() for path in self.profiles))

    def test_failed_revocation_preserves_exports_and_does_not_restart(self):
        self.pki.revoke_client.side_effect = RuntimeError("revocation failed")
        for entrypoint in ("cli", "panel", "request"):
            with self.subTest(entrypoint=entrypoint):
                with self.assertRaisesRegex(RuntimeError, "revocation failed"):
                    self._revoke(entrypoint)
                for path in self.profiles:
                    self.assertEqual("revoked client fixture", path.read_text(encoding="utf-8"))
                self.sync.assert_not_called()
                self.supervisor.restart.assert_not_called()

    def test_panel_restarts_even_if_export_cleanup_fails(self):
        with patch.object(main, "remove_profile", side_effect=PermissionError("cleanup failed")):
            with self.assertRaisesRegex(PermissionError, "cleanup failed"):
                self._revoke("panel")
        self.supervisor.restart.assert_called_once_with("certificate revoked")


if __name__ == "__main__":
    unittest.main()
