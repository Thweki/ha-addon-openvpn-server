import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import openvpn_addon.config as config_module
from openvpn_addon.config import AddonConfig
from openvpn_addon.pki import PkiManager


class ClientAddressTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.original_root = config_module.DATA_ROOT
        config_module.DATA_ROOT = Path(self.tempdir.name)
        self.addCleanup(setattr, config_module, "DATA_ROOT", self.original_root)
        self.config = AddonConfig(local_networks=["192.168.1.0/24"])
        self.pki = PkiManager(self.config)

    def test_addresses_start_after_the_server_address(self):
        assignments = self.pki.ensure_client_ips(["alice", "bob"])
        self.assertEqual({"alice": "10.8.0.2", "bob": "10.8.0.3"}, assignments)
        self.assertEqual("10.8.0.2", json.loads(self.config.client_ips_path.read_text())["alice"])
        self.assertEqual(
            "ifconfig-push 10.8.0.2 255.255.255.0\n",
            (self.config.ccd_dir / "alice").read_text(),
        )

    def test_assignments_are_stable_and_stale_entries_are_pruned(self):
        self.pki.ensure_client_ips(["alice", "bob"])
        again = self.pki.ensure_client_ips(["alice", "bob"])
        self.assertEqual("10.8.0.2", again["alice"])

        self.pki.ensure_client_ips(["alice"])
        self.assertFalse((self.config.ccd_dir / "bob").exists())
        self.assertNotIn("bob", json.loads(self.config.client_ips_path.read_text()))

    def test_released_address_is_reused(self):
        self.pki.ensure_client_ips(["alice", "bob"])
        self.pki.ensure_client_ips(["alice"])
        reassigned = self.pki.ensure_client_ips(["alice", "carol"])
        self.assertEqual("10.8.0.3", reassigned["carol"])

    def test_unchanged_ccd_files_are_not_rewritten(self):
        names = [f"client-{number}" for number in range(100)]
        expected = self.pki.ensure_client_ips(names)
        with patch.object(Path, "write_text", autospec=True, side_effect=Path.write_text) as write:
            self.assertEqual(expected, self.pki.ensure_client_ips(names))
        self.assertEqual([self.config.client_ips_path], [call.args[0] for call in write.call_args_list])

    def test_adding_a_client_only_writes_its_ccd_file(self):
        self.pki.ensure_client_ips(["alice"])
        with patch.object(Path, "write_text", autospec=True, side_effect=Path.write_text) as write:
            assignments = self.pki.ensure_client_ips(["alice", "bob"])
        self.assertEqual(
            [self.config.client_ips_path, self.config.ccd_dir / "bob"],
            [call.args[0] for call in write.call_args_list],
        )
        self.assertEqual("10.8.0.3", assignments["bob"])
        self.assertEqual("ifconfig-push 10.8.0.3 255.255.255.0\n", (self.config.ccd_dir / "bob").read_text())

    def test_sync_restores_missing_or_modified_ccd_files(self):
        self.pki.ensure_client_ips(["alice", "bob"])
        (self.config.ccd_dir / "alice").unlink()
        (self.config.ccd_dir / "bob").write_bytes(b"\xff")
        self.pki.ensure_client_ips(["alice", "bob"])
        for name, address in [("alice", "10.8.0.2"), ("bob", "10.8.0.3")]:
            self.assertEqual(
                f"ifconfig-push {address} 255.255.255.0\n",
                (self.config.ccd_dir / name).read_text(),
            )

    def test_netmask_change_updates_ccd_files(self):
        self.pki.ensure_client_ips(["alice"])
        self.config.vpn_netmask = "255.255.255.128"
        self.pki.ensure_client_ips(["alice"])
        self.assertEqual(
            "ifconfig-push 10.8.0.2 255.255.255.128\n",
            (self.config.ccd_dir / "alice").read_text(),
        )

    def test_unreadable_ccd_file_still_gets_a_write_attempt(self):
        self.pki.ensure_client_ips(["alice"])
        with (
            patch.object(Path, "read_bytes", side_effect=PermissionError),
            patch.object(Path, "write_text", autospec=True, side_effect=Path.write_text) as write,
        ):
            self.pki.ensure_client_ips(["alice"])
        self.assertIn(self.config.ccd_dir / "alice", [call.args[0] for call in write.call_args_list])

    def test_shared_certificate_mode_skips_ccd(self):
        self.pki.ensure_client_ips(["alice"])
        self.config.duplicate_cn = True
        assignments = self.pki.ensure_client_ips(["alice"])
        self.assertEqual({}, assignments)
        self.assertEqual([], list(self.config.ccd_dir.iterdir()))

    def test_exhausted_network_is_reported(self):
        self.config.vpn_subnet = "10.9.0.0"
        self.config.vpn_netmask = "255.255.255.252"
        with self.assertRaises(RuntimeError):
            self.pki.ensure_client_ips(["alice", "bob"])

    def test_uses_client_config_dir_follows_both_options(self):
        self.assertTrue(self.config.uses_client_config_dir)
        self.config.duplicate_cn = True
        self.assertFalse(self.config.uses_client_config_dir)
        self.config.duplicate_cn = False
        self.config.fixed_client_ips = False
        self.assertFalse(self.config.uses_client_config_dir)


if __name__ == "__main__":
    unittest.main()
