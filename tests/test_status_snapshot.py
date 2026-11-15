import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from openvpn_addon.config import AddonConfig
from openvpn_addon.health import collect_health
from openvpn_addon.network import NetworkResult
from openvpn_addon.pki import CrlInfo, PkiManager
from openvpn_addon.status import collect_status


class StatusSnapshotTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for name in ("DATA_ROOT", "SHARE_ROOT"):
            patcher = patch(f"openvpn_addon.config.{name}", root / name)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.config = AddonConfig(public_endpoint="vpn.example.com")
        self.pki = Mock(spec=PkiManager)
        self.pki.list_clients.return_value = []
        self.verify = Mock()
        for module in ("health", "status"):
            patcher = patch(f"openvpn_addon.{module}.verify_network", self.verify)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_status_collects_once_and_refreshes_on_the_next_request(self):
        snapshots = [
            (NetworkResult(ip_forward=True, out_interface="eth0"),
             CrlInfo("previous update", "next update", 120), "ok"),
            (NetworkResult(ip_forward=False, out_interface="eth1", missing_rules=["MASQUERADE rule"]),
             CrlInfo("new update", "expired update", -1), "error"),
        ]
        for count, (network, crl, expected_health) in enumerate(snapshots, start=1):
            with self.subTest(request=count):
                self.verify.return_value = network
                self.pki.crl_info.return_value = crl
                data = collect_status(self.config, self.pki)

                self.assertEqual(count, self.verify.call_count)
                self.assertEqual(count, self.pki.crl_info.call_count)
                self.assertEqual(network.ip_forward, data["networking"]["ip_forward"])
                self.assertEqual(network.out_interface, data["networking"]["out_interface"])
                self.assertEqual({"last_update": crl.last_update, "next_update": crl.next_update,
                                  "days_remaining": crl.days_remaining}, data["crl"])
                checks = {item["name"]: item for item in data["health"]["checks"]}
                for name in ("ip_forward", "lan_forwarding", "crl"):
                    self.assertEqual(expected_health, checks[name]["status"])
                self.assertIn(crl.next_update, checks["crl"]["detail"])

    def test_status_reuses_missing_crl_and_unavailable_network_results(self):
        self.verify.return_value = NetworkResult(errors=["iptables unavailable"])
        self.pki.crl_info.return_value = CrlInfo()

        data = collect_status(self.config, self.pki)

        self.verify.assert_called_once_with(self.config)
        self.pki.crl_info.assert_called_once_with()
        self.assertFalse(data["networking"]["ip_forward"])
        self.assertIsNone(data["networking"]["out_interface"])
        self.assertIsNone(data["crl"]["next_update"])
        checks = {item["name"]: item for item in data["health"]["checks"]}
        self.assertEqual("iptables unavailable", checks["lan_forwarding"]["detail"])
        self.assertEqual("error", checks["crl"]["status"])

    def test_standalone_health_still_collects_fresh_results(self):
        self.verify.return_value = NetworkResult(ip_forward=True)
        self.pki.crl_info.side_effect = [CrlInfo(next_update="soon", days_remaining=5), CrlInfo()]

        first = {item.name: item for item in collect_health(self.config, self.pki)}
        second = {item.name: item for item in collect_health(self.config, self.pki)}

        self.assertEqual(2, self.verify.call_count)
        self.assertEqual(2, self.pki.crl_info.call_count)
        self.assertEqual("warn", first["crl"].status)
        self.assertEqual("error", second["crl"].status)


if __name__ == "__main__":
    unittest.main()
