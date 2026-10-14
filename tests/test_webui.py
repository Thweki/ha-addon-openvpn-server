import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import openvpn_addon.config as config_module
from openvpn_addon.config import AddonConfig
from openvpn_addon.validator import validate_client_name
from openvpn_addon.webui import PanelActions, PanelServer, _match_client_route


class RouteTests(unittest.TestCase):
    def test_matches_client_routes(self):
        self.assertEqual("alice", _match_client_route("/api/clients/alice/profile", "/api/clients/", "/profile"))
        self.assertEqual("alice", _match_client_route("/api/clients/alice/revoke", "/api/clients/", "/revoke"))

    def test_rejects_traversal_and_other_paths(self):
        self.assertIsNone(_match_client_route("/api/clients/a/b/profile", "/api/clients/", "/profile"))
        self.assertIsNone(_match_client_route("/api/clients//profile", "/api/clients/", "/profile"))
        self.assertIsNone(_match_client_route("/api/status", "/api/clients/", "/profile"))


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.original_root = config_module.DATA_ROOT
        self.original_share = config_module.SHARE_ROOT
        config_module.DATA_ROOT = root
        config_module.SHARE_ROOT = root / "share"
        self.addCleanup(setattr, config_module, "DATA_ROOT", self.original_root)
        self.addCleanup(setattr, config_module, "SHARE_ROOT", self.original_share)

        self.config = AddonConfig(server_host="192.168.1.6", public_endpoint="vpn.example.com")
        self.config.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.issued: list[str] = []

        def issue(name):
            validate_client_name(name)
            self.issued.append(name)
            return {"status": "ok", "client_name": name, "action": "created"}

        def revoke(name):
            validate_client_name(name)
            return {"status": "ok", "client_name": name, "message": "revoked"}

        def profile(name):
            if name == "ghost":
                raise ValueError("unknown client: ghost")
            return "client\nremote vpn.example.com 1194\n"

        actions = PanelActions(
            issue_client=issue,
            revoke_client=revoke,
            export_profile=profile,
            refresh_crl=lambda: {"status": "ok", "next_update": "Jan 1 00:00:00 2030 GMT"},
            restart_openvpn=lambda: {"status": "ok", "message": "restarted"},
            runtime_info=lambda: {"openvpn_running": True},
        )
        self.panel = PanelServer(self.config, actions, "127.0.0.1", 0)
        self.panel.start()
        self.addCleanup(self.panel.stop)
        self.base = f"http://127.0.0.1:{self.panel.address[1]}"

    def request(self, path, payload=None, method=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=data, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode("utf-8"), dict(response.headers)
        except urllib.error.HTTPError as error:
            try:
                return error.code, error.read().decode("utf-8"), dict(error.headers)
            finally:
                error.close()

    def test_status_endpoint(self):
        status, body, _ = self.request("/api/status")
        self.assertEqual(200, status)
        self.assertEqual("vpn.example.com:1194", json.loads(body)["summary"]["public_address"])

    def test_page_is_served(self):
        status, body, _ = self.request("/")
        self.assertEqual(200, status)
        self.assertIn("OpenVPN Server", body)

    def test_issue_client(self):
        status, body, _ = self.request("/api/clients", {"client_name": "alice"}, method="POST")
        self.assertEqual(200, status)
        self.assertEqual("created", json.loads(body)["action"])
        self.assertEqual(["alice"], self.issued)

    def test_invalid_client_name_is_a_bad_request(self):
        status, body, _ = self.request("/api/clients", {"client_name": "bad name"}, method="POST")
        self.assertEqual(400, status)
        self.assertIn("error", json.loads(body)["status"])

    def test_invalid_json_body_is_a_bad_request(self):
        request = urllib.request.Request(self.base + "/api/clients", data=b"{nope", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(400, context.exception.code)

    def test_profile_download(self):
        status, body, headers = self.request("/api/clients/alice/profile")
        self.assertEqual(200, status)
        self.assertIn("remote vpn.example.com 1194", body)
        self.assertIn("alice.ovpn", headers.get("Content-Disposition", ""))

    def test_unknown_client_profile_is_reported(self):
        status, body, _ = self.request("/api/clients/ghost/profile")
        self.assertEqual(400, status)
        self.assertIn("unknown client", json.loads(body)["message"])

    def test_revoke_client(self):
        status, body, _ = self.request("/api/clients/alice/revoke", {}, method="POST")
        self.assertEqual(200, status)
        self.assertEqual("revoked", json.loads(body)["message"])

    def test_maintenance_endpoints(self):
        for path in ("/api/crl/refresh", "/api/openvpn/restart"):
            status, _, _ = self.request(path, {}, method="POST")
            self.assertEqual(200, status, path)

    def test_diagnostics_bundle(self):
        status, body, _ = self.request("/api/diagnostics")
        self.assertEqual(200, status)
        self.assertIn("OpenVPN add-on diagnostics", body)
        self.assertIn("## status", body)

    def test_unknown_path_is_not_found(self):
        status, _, _ = self.request("/api/nope")
        self.assertEqual(404, status)


class PanelBindingTests(unittest.TestCase):
    """The panel must be reachable from Home Assistant but never from the LAN."""

    def setUp(self):
        import ipaddress

        from openvpn_addon import network, webui

        self.ipaddress = ipaddress
        self.network = network
        self.webui = webui
        self.original = (network._host_ipv4_addresses, network._default_interface)
        self.addCleanup(self._restore)

    def _restore(self):
        self.network._host_ipv4_addresses, self.network._default_interface = self.original

    def _interfaces(self, pairs):
        self.network._host_ipv4_addresses = lambda: [
            (name, self.ipaddress.IPv4Interface(value)) for name, value in pairs
        ]

    def _default(self, name):
        self.network._default_interface = lambda: name

    def test_binds_internal_addresses_and_skips_the_lan(self):
        self._interfaces(
            [
                ("lo", "127.0.0.1/8"),
                ("enp1s0", "192.168.1.6/24"),
                ("hassio", "172.30.32.1/23"),
                ("docker0", "172.30.232.1/24"),
                ("dummy0", "169.254.10.1/16"),
            ]
        )
        self._default("enp1s0")
        config = AddonConfig(local_networks=["192.168.1.0/24"])
        self.assertEqual(
            ["172.30.32.1", "172.30.232.1", "127.0.0.1"],
            self.webui.panel_bind_addresses(config),
        )

    def test_skips_addresses_inside_configured_lan_networks(self):
        self._interfaces([("enp1s0", "10.10.0.5/24"), ("br0", "10.20.0.1/24")])
        self._default("enp1s0")
        config = AddonConfig(local_networks=["10.10.0.0/24"])
        self.assertEqual(["10.20.0.1", "127.0.0.1"], self.webui.panel_bind_addresses(config))

    def test_falls_back_to_all_interfaces_with_a_warning_path(self):
        self._interfaces([("lo", "127.0.0.1/8"), ("enp1s0", "192.168.1.6/24")])
        self._default("enp1s0")
        config = AddonConfig(local_networks=["192.168.1.0/24"])
        self.assertEqual(["0.0.0.0"], self.webui.panel_bind_addresses(config))


if __name__ == "__main__":
    unittest.main()
