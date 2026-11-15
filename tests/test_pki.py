import json
import tempfile
import unittest
from pathlib import Path

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

    def test_subnet_change_reallocates_saved_clients_alongside_new_clients(self):
        self.pki.ensure_client_ips(["alice", "bob"])
        self.config.vpn_subnet = "10.9.0.0"

        assignments = self.pki.ensure_client_ips(["alice", "bob", "carol"])

        self.assertEqual(
            {"alice": "10.9.0.2", "bob": "10.9.0.3", "carol": "10.9.0.4"},
            assignments,
        )
        self.assertEqual(assignments, json.loads(self.config.client_ips_path.read_text()))
        for name, address in assignments.items():
            self.assertEqual(
                f"ifconfig-push {address} 255.255.255.0\n",
                (self.config.ccd_dir / name).read_text(),
            )

    def test_smaller_network_preserves_valid_hosts_and_reallocates_other_clients(self):
        self.pki.ensure_client_ips(["alice", "bob", "carol", "dave", "erin", "frank", "grace"])
        self.config.vpn_netmask = "255.255.255.248"

        assignments = self.pki.ensure_client_ips(["frank", "grace", "alice", "bob", "carol"])

        self.assertEqual(
            {
                "alice": "10.8.0.2", "bob": "10.8.0.3", "carol": "10.8.0.4",
                "frank": "10.8.0.5", "grace": "10.8.0.6",
            },
            assignments,
        )
        self.assertEqual(assignments, json.loads(self.config.client_ips_path.read_text()))
        self.assertFalse((self.config.ccd_dir / "erin").exists())
        for name, address in assignments.items():
            self.assertEqual(
                f"ifconfig-push {address} 255.255.255.248\n",
                (self.config.ccd_dir / name).read_text(),
            )

    def test_invalid_saved_addresses_are_reallocated_without_displacing_valid_clients(self):
        self.config.client_ips_path.parent.mkdir(parents=True, exist_ok=True)
        for invalid in (
            "not-an-address",
            "2001:db8::2",
            "10.9.0.2",
            "10.8.0.0",
            "10.8.0.1",
            "10.8.0.255",
        ):
            with self.subTest(address=invalid):
                self.config.client_ips_path.write_text(json.dumps({"alice": invalid, "bob": "10.8.0.2"}))

                assignments = self.pki.ensure_client_ips(["alice", "bob"])

                self.assertEqual({"alice": "10.8.0.3", "bob": "10.8.0.2"}, assignments)
                self.assertEqual(assignments, json.loads(self.config.client_ips_path.read_text()))
                self.assertEqual(
                    "ifconfig-push 10.8.0.3 255.255.255.0\n",
                    (self.config.ccd_dir / "alice").read_text(),
                )

    def test_duplicate_saved_addresses_are_reallocated(self):
        self.config.client_ips_path.parent.mkdir(parents=True, exist_ok=True)
        self.config.client_ips_path.write_text(
            json.dumps({"alice": "10.8.0.2", "bob": "10.8.0.2", "carol": "10.8.0.3"})
        )

        assignments = self.pki.ensure_client_ips(["alice", "bob", "carol"])

        self.assertEqual({"alice": "10.8.0.2", "bob": "10.8.0.4", "carol": "10.8.0.3"}, assignments)
        self.assertEqual(assignments, json.loads(self.config.client_ips_path.read_text()))
        self.assertEqual(
            "ifconfig-push 10.8.0.4 255.255.255.0\n",
            (self.config.ccd_dir / "bob").read_text(),
        )

    def test_exhaustion_during_reconciliation_leaves_saved_state_unchanged(self):
        self.pki.ensure_client_ips(["alice", "bob"])
        saved = self.config.client_ips_path.read_text()
        ccd = {path.name: path.read_text() for path in self.config.ccd_dir.iterdir()}
        self.config.vpn_netmask = "255.255.255.252"

        for subnet in ("10.8.0.0", "10.9.0.0"):
            with self.subTest(subnet=subnet):
                self.config.vpn_subnet = subnet
                with self.assertRaisesRegex(RuntimeError, "no free address for client bob"):
                    self.pki.ensure_client_ips(["alice", "bob"])

                self.assertEqual(saved, self.config.client_ips_path.read_text())
                self.assertEqual(ccd, {path.name: path.read_text() for path in self.config.ccd_dir.iterdir()})

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
