import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openvpn_addon.client_network import check_client_networks, parse_client_network
from openvpn_addon.config import AddonConfig


class ClientNetworkTests(unittest.TestCase):
    def check(self, config, *networks):
        return check_client_networks(config, [parse_client_network(value) for value in networks])

    def test_disjoint_and_adjacent_networks_are_clear(self):
        report = self.check(AddonConfig(local_networks=["192.168.1.0/24"]),
                            "192.168.2.0/24", "10.8.1.0/24")
        self.assertEqual("ok", report["status"])
        self.assertEqual([], report["conflicts"])
        self.assertEqual(2, len(report["checked_networks"]))

    def test_equal_lan_reports_metric_risk_and_remedy(self):
        report = self.check(AddonConfig(local_networks=["192.168.1.0/24"]), "192.168.1.42/24")
        self.assertEqual("warn", report["status"])
        conflict, = report["conflicts"]
        self.assertEqual("pushed_route", conflict["kind"])
        self.assertEqual("192.168.1.0/24", conflict["overlap"])
        self.assertIn("metrics", conflict["risk"])
        self.assertIn("non-overlapping", conflict["recommendation"])

    def test_client_lan_inside_pushed_route(self):
        report = self.check(AddonConfig(push_routes=["172.16.0.0/16"]), "172.16.4.0/24")
        conflict, = report["conflicts"]
        self.assertEqual("172.16.4.0/24", conflict["overlap"])
        self.assertIn("Client LAN is more specific", conflict["risk"])

    def test_pushed_host_route_inside_client_lan(self):
        report = self.check(AddonConfig(push_routes=["172.16.4.8/32"]), "172.16.4.0/24")
        conflict, = report["conflicts"]
        self.assertEqual("172.16.4.8/32", conflict["overlap"])
        self.assertIn("Pushed route is more specific", conflict["risk"])

    def test_vpn_pool_conflicts_use_configured_netmask(self):
        config = AddonConfig(vpn_subnet="10.16.0.0", vpn_netmask="255.255.0.0")
        report = self.check(config, "10.16.4.0/24")
        conflict, = report["conflicts"]
        self.assertEqual("vpn_subnet", conflict["kind"])
        self.assertEqual("10.16.0.0/16", conflict["vpn_network"])
        self.assertEqual("10.16.4.0/24", conflict["overlap"])
        self.assertIn("Choose a VPN subnet", conflict["recommendation"])

    def test_multiple_networks_and_sources_are_all_checked_once(self):
        config = AddonConfig(local_networks=["192.168.1.0/24"],
                             push_routes=["192.168.1.42/24", "172.16.0.0/16"])
        report = self.check(config, "192.168.1.42/24", "192.168.1.0/24", "172.16.4.0/24", "10.8.0.0/24")
        self.assertEqual(3, len(report["client_networks"]))
        self.assertEqual(3, len(report["checked_networks"]))
        self.assertEqual(3, len(report["conflicts"]))
        self.assertEqual(["pushed_route", "pushed_route", "vpn_subnet"],
                         [item["kind"] for item in report["conflicts"]])

    def test_nat_and_redirect_gateway_do_not_hide_conflicts(self):
        config = AddonConfig(local_networks=["192.168.1.0/24"], nat_enabled=False, redirect_gateway=True)
        self.assertEqual("warn", self.check(config, "192.168.1.0/24")["status"])
        clear = self.check(config, "192.168.2.0/24")
        self.assertEqual("ok", clear["status"])
        self.assertIn("redirect_gateway", " ".join(clear["notes"]))

    def test_invalid_networks_require_ipv4_and_a_mask(self):
        for value in ("192.168.1.1", "not-a-network", "192.168.1.0/33", "::/64"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_client_network(value)

    def test_analysis_needs_no_files_or_subprocesses(self):
        with patch("builtins.open", side_effect=AssertionError("unexpected file access")), \
             patch("pathlib.Path.open", side_effect=AssertionError("unexpected file access")), \
             patch("subprocess.run", side_effect=AssertionError("unexpected subprocess")):
            self.assertEqual("warn", self.check(AddonConfig(), "10.8.0.0/24")["status"])


class ClientNetworkCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.options = self.root / "options.json"
        self.options.write_text(json.dumps({"lan_networks": "192.168.1.0/24",
                                            "extra_routes": "172.16.0.0/16"}), encoding="utf-8")
        self.env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "addon/src"),
                        OPENVPN_ADDON_OPTIONS=str(self.options), OPENVPN_ADDON_DATA=str(self.root / "data"),
                        OPENVPN_ADDON_SHARE=str(self.root / "share"), PYTHONDONTWRITEBYTECODE="1")

    def run_cli(self, *args):
        result = subprocess.run([sys.executable, "-m", "openvpn_addon.main", "check-client-network", *args],
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(["options.json"], sorted(path.name for path in self.root.iterdir()))
        return result

    def test_cli_success_and_warning_codes_with_real_options(self):
        for network, code, count in (("192.168.2.0/24", 0, 0), ("192.168.1.0/24", 1, 1),
                                     ("172.16.1.0/24", 1, 1)):
            with self.subTest(network=network):
                result = self.run_cli(network)
                self.assertEqual(code, result.returncode, result.stderr)
                self.assertEqual(count, len(json.loads(result.stdout)["conflicts"]))
                self.assertEqual("", result.stderr)

    def test_cli_checks_multiple_lans(self):
        result = self.run_cli("192.168.1.10/24", "10.8.0.10/24")
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertEqual(2, len(json.loads(result.stdout)["conflicts"]))

    def test_cli_rejects_missing_or_invalid_subnets(self):
        for args in ([], ["192.168.1.1"], ["::/64"], ["192.168.1.0/33"]):
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(2, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertIn("error:", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_cli_invalid_configuration_is_an_error_not_a_clear_report(self):
        for content in ('{"vpn_netmask": "bad-mask"}', '{"listen_port": "bad-port"}', '{'):
            with self.subTest(content=content):
                self.options.write_text(content, encoding="utf-8")
                result = self.run_cli("192.168.2.0/24")
                self.assertEqual(2, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertEqual("Unable to load or validate add-on configuration.\n", result.stderr)


if __name__ == "__main__":
    unittest.main()
