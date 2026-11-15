import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import openvpn_addon.config as config_module
from openvpn_addon.config import AddonConfig
from openvpn_addon.pki import ClientInfo, PkiManager


class ClientListingTests(unittest.TestCase):
    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = patch.object(config_module, "DATA_ROOT", Path(tempdir.name))
        root.start()
        self.addCleanup(root.stop)
        self.config = AddonConfig()
        self.pki = PkiManager(self.config)
        self.config.pki_dir.mkdir()
        self.index = self.config.pki_dir / "index.txt"

    def write_index(self, entries):
        self.index.write_text(
            "".join(
                f"{status}\t301231235959Z\t\t{serial:02X}\tunknown\t/CN={name}\n"
                for serial, (name, status) in enumerate(entries, start=1)
            ),
            encoding="utf-8",
        )

    def test_listing_reads_index_once_for_many_clients(self):
        names = [f"client-{number:03d}" for number in range(128)]
        self.write_index([(name, "V") for name in names])

        with patch.object(Path, "read_text", autospec=True, side_effect=Path.read_text) as read:
            clients = self.pki.list_clients()

        self.assertEqual([ClientInfo(name, "valid") for name in names], clients)
        self.assertEqual(1, read.call_count, "a listing must read the PKI index only once")

    def test_listing_merges_index_and_issued_files_preserving_status_and_expiry(self):
        self.write_index([("revoked", "R"), ("server", "V"), ("alice", "V")])
        issued = self.config.pki_dir / "issued"
        issued.mkdir()
        for name in ("server", "alice", "revoked", "legacy"):
            (issued / f"{name}.crt").touch()
        expiry = "Dec 31 23:59:59 2030 GMT"
        with patch.object(self.pki, "_cert_expiry", return_value=expiry) as get_expiry:
            clients = self.pki.list_clients()

        self.assertEqual(
            [ClientInfo("alice", "valid", expiry), ClientInfo("legacy", "valid", expiry),
             ClientInfo("revoked", "revoked", expiry)],
            clients,
        )
        self.assertEqual([("alice",), ("legacy",), ("revoked",)],
                         [call.args for call in get_expiry.call_args_list])
        self.assertEqual("valid", self.pki.client_status("legacy"))
        self.assertEqual("missing", self.pki.client_status("unknown"))

    def test_subsequent_listings_and_status_lookups_read_fresh_index(self):
        self.write_index([("alice", "V"), ("removed", "V")])
        self.assertEqual([ClientInfo("alice", "valid"), ClientInfo("removed", "valid")],
                         self.pki.list_clients())
        self.assertEqual("valid", self.pki.client_status("alice"))

        self.write_index([("alice", "R"), ("bob", "V")])
        self.assertEqual([ClientInfo("alice", "revoked"), ClientInfo("bob", "valid")],
                         self.pki.list_clients())
        self.assertEqual("revoked", self.pki.client_status("alice"))
        self.assertEqual("missing", self.pki.client_status("removed"))

    def test_missing_index_keeps_issued_file_fallback(self):
        self.assertEqual([], self.pki.list_clients())
        issued = self.config.pki_dir / "issued"
        issued.mkdir()
        (issued / "legacy.crt").touch()
        (issued / "server.crt").touch()
        with patch.object(self.pki, "_cert_expiry", return_value=None):
            self.assertEqual([ClientInfo("legacy", "valid")], self.pki.list_clients())


if __name__ == "__main__":
    unittest.main()
