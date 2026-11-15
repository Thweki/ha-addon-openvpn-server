import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import openvpn_addon.config as config_module
from openvpn_addon.config import AddonConfig
from openvpn_addon.main import sync_client_ips
from openvpn_addon.pki import PkiManager


class ClientSyncTests(unittest.TestCase):
    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root_patch = patch.object(config_module, "DATA_ROOT", Path(tempdir.name))
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.config = AddonConfig()
        self.pki = PkiManager(self.config)
        issued = self.config.pki_dir / "issued"
        issued.mkdir(parents=True)
        for name in ("alice", "bob", "orphan", "server"):
            (issued / f"{name}.crt").write_text("dummy certificate", encoding="utf-8")
        self.write_index("V")

    def write_index(self, alice_status):
        (self.config.pki_dir / "index.txt").write_text(
            f"{alice_status}\t280101000000Z\t\t01\tunknown\t/CN=alice\n"
            "R\t280101000000Z\t261101000000Z\t02\tunknown\t/CN=bob\n"
            "V\t280101000000Z\t\t03\tunknown\t/CN=server\n",
            encoding="utf-8",
        )

    def test_listing_without_expiry_preserves_names_and_statuses(self):
        with patch.object(self.pki, "_cert_expiry") as expiry:
            clients = self.pki.list_clients(include_expiry=False)
        expiry.assert_not_called()
        self.assertEqual(
            [("alice", "valid", None), ("bob", "revoked", None), ("orphan", "valid", None)],
            [(client.name, client.status, client.expires) for client in clients],
        )

    def test_default_listing_still_reads_expiry(self):
        expires = "Jan  1 00:00:00 2028 GMT"
        completed = Mock(returncode=0, stdout=f"notAfter={expires}\n")
        with patch("openvpn_addon.pki.subprocess.run", return_value=completed) as run:
            clients = self.pki.list_clients()
        self.assertEqual(3, run.call_count)
        self.assertEqual([expires] * 3, [client.expires for client in clients])

    def test_address_sync_does_not_launch_certificate_processes(self):
        with patch("openvpn_addon.pki.subprocess.run") as run:
            with contextlib.redirect_stdout(io.StringIO()):
                assignments = sync_client_ips(self.config, self.pki)
        run.assert_not_called()
        self.assertEqual({"alice": "10.8.0.2", "orphan": "10.8.0.3"}, assignments)
        self.assertEqual(
            "ifconfig-push 10.8.0.2 255.255.255.0\n",
            (self.config.ccd_dir / "alice").read_text(encoding="utf-8"),
        )
        self.assertFalse((self.config.ccd_dir / "bob").exists())

    def test_next_sync_observes_revocation_and_removes_stale_ccd(self):
        with patch("openvpn_addon.pki.subprocess.run") as run:
            with contextlib.redirect_stdout(io.StringIO()):
                sync_client_ips(self.config, self.pki)
                self.write_index("R")
                assignments = sync_client_ips(self.config, self.pki)
        run.assert_not_called()
        self.assertEqual({"orphan": "10.8.0.3"}, assignments)
        self.assertFalse((self.config.ccd_dir / "alice").exists())


if __name__ == "__main__":
    unittest.main()
