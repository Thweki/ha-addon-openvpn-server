import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from openvpn_addon.config import AddonConfig
from openvpn_addon.health import collect_health, worst_status
from openvpn_addon.network import NetworkResult
from openvpn_addon.pki import ClientInfo, CrlInfo, PkiManager
from openvpn_addon.status import status_json


NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)


def expiry(**delta):
    return (NOW + timedelta(**delta)).strftime("%b %d %H:%M:%S %Y GMT")


class ClientExpiryTests(unittest.TestCase):
    def setUp(self):
        self.config = AddonConfig(
            public_endpoint="vpn.example.com", local_networks=["192.168.1.0/24"]
        )
        self.pki = Mock(spec=PkiManager)
        self.pki.list_clients.return_value = []
        self.pki.crl_info.return_value = CrlInfo(next_update="future", days_remaining=100)
        self.enterContext(patch("openvpn_addon.health.verify_network", return_value=NetworkResult(
            applied=True, ip_forward=True, out_interface="eth0"
        )))
        clock = self.enterContext(patch("openvpn_addon.health.datetime", wraps=datetime, create=True))
        clock.now.return_value = NOW

    def checks(self, *clients):
        self.pki.list_clients.return_value = list(clients)
        return collect_health(self.config, self.pki)

    def test_warns_at_thirty_days_with_replacement_guidance(self):
        checks = self.checks(ClientInfo("phone", "valid", expiry(days=30)))
        self.assertEqual("warn", worst_status(checks))
        detail = next(check.detail for check in checks if check.status == "warn")
        self.assertIn("phone", detail)
        self.assertIn("30 day(s)", detail)
        self.assertIn("new name", detail)

    def test_more_than_thirty_days_is_healthy(self):
        checks = self.checks(ClientInfo("phone", "valid", expiry(days=30, seconds=1)))
        self.assertEqual("ok", worst_status(checks))

    def test_less_than_one_day_is_warning_not_expired(self):
        checks = self.checks(ClientInfo("phone", "valid", expiry(seconds=1)))
        self.assertEqual("warn", worst_status(checks))
        detail = next(check.detail for check in checks if check.status == "warn")
        self.assertIn("1 day(s)", detail)
        self.assertNotIn("has expired", detail)

    def test_expired_at_or_before_current_time_is_error(self):
        for seconds in (0, -1, -86400):
            with self.subTest(seconds=seconds):
                checks = self.checks(ClientInfo("phone", "valid", expiry(seconds=seconds)))
                self.assertEqual("error", worst_status(checks))
                detail = next(check.detail for check in checks if check.status == "error")
                self.assertIn("phone", detail)
                self.assertIn("has expired", detail)

    def test_revoked_certificates_do_not_raise_expiry_alerts(self):
        for expires in (None, "invalid", expiry(days=-1), expiry(days=2)):
            with self.subTest(expires=expires):
                self.assertEqual("ok", worst_status(self.checks(ClientInfo("old", "revoked", expires))))

    def test_unknown_expiry_is_visible_without_crashing(self):
        for expires in (None, "", "not a certificate date", "Nov 99 12:00:00 2026 GMT"):
            with self.subTest(expires=expires):
                checks = self.checks(ClientInfo("phone", "valid", expires))
                self.assertEqual("warn", worst_status(checks))
                detail = next(check.detail for check in checks if check.status == "warn")
                self.assertIn("phone", detail)
                self.assertIn("unknown", detail)

    def test_no_clients_is_healthy(self):
        self.assertEqual("ok", worst_status(self.checks()))

    def test_status_json_exposes_alerts_and_uses_one_client_snapshot(self):
        self.pki.list_clients.return_value = [
            ClientInfo("phone", "valid", expiry(days=7)),
            ClientInfo("laptop", "valid", expiry(days=-1)),
            ClientInfo("retired", "revoked", expiry(days=-2)),
        ]
        with tempfile.TemporaryDirectory() as root:
            self.enterContext(patch("openvpn_addon.config.DATA_ROOT", Path(root)))
            self.enterContext(patch("openvpn_addon.status._network_snapshot", return_value={
                "ip_forward": True, "out_interface": "eth0"
            }))
            data = json.loads(status_json(self.config, self.pki))
        self.assertEqual("error", data["health"]["status"])
        alerts = [check for check in data["health"]["checks"] if check["status"] != "ok"]
        self.assertEqual(["warn", "error"], [check["status"] for check in alerts])
        self.assertIn("phone", alerts[0]["detail"])
        self.assertIn("laptop", alerts[1]["detail"])
        self.assertEqual(3, len(data["clients"]))
        self.pki.list_clients.assert_called_once_with()
        self.pki.ensure_client.assert_not_called()
        self.pki.revoke_client.assert_not_called()
        self.pki.refresh_crl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
