import http.client
import io
import ipaddress
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import openvpn_addon.config as config_module
from openvpn_addon.config import AddonConfig
from openvpn_addon.validator import validate_client_name
from openvpn_addon.webui import PanelActions, _build_handler, _match_client_route, panel_bind_addresses


class MemoryConnection:
    """Exercise the real HTTP parser and handler without network privileges."""

    def __init__(self, data):
        self.data = data
        self.sent = bytearray()

    def makefile(self, *args):
        return io.BytesIO(self.data)

    def sendall(self, data):
        self.sent.extend(data)


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

        self.actions = PanelActions(
            issue_client=Mock(side_effect=issue),
            revoke_client=Mock(side_effect=revoke),
            export_profile=Mock(side_effect=profile),
            refresh_crl=Mock(return_value={"status": "ok", "next_update": "Jan 1 00:00:00 2030 GMT"}),
            restart_openvpn=Mock(return_value={"status": "ok", "message": "restarted"}),
            runtime_info=Mock(return_value={"openvpn_running": True}),
        )
        self.handler = _build_handler(self.config, self.actions)

    def request(self, path, payload=None, method=None, peer="172.30.32.2", headers=None, raw_body=None):
        data = b"" if payload is None else json.dumps(payload).encode("utf-8")
        if raw_body is not None:
            data = raw_body
        method = method or ("GET" if payload is None else "POST")
        request = f"{method} {path} HTTP/1.0\r\nContent-Length: {len(data)}\r\n"
        for name, value in (headers or {}).items():
            request += f"{name}: {value}\r\n"
        connection = MemoryConnection(request.encode("ascii") + b"\r\n" + data)
        self.handler(connection, (peer, 12345), Mock())
        response = http.client.HTTPResponse(MemoryConnection(bytes(connection.sent)), method=method)
        response.begin()
        with response:
            return response.status, response.read().decode("utf-8"), dict(response.headers)

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
        status, _, _ = self.request("/api/clients", raw_body=b"{nope", method="POST")
        self.assertEqual(400, status)

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

    def test_only_supervisor_peer_can_access_any_endpoint(self):
        routes = [
            ("GET", "/"),
            ("GET", "/api/status"),
            ("GET", "/api/diagnostics"),
            ("GET", "/api/clients/alice/profile"),
            ("POST", "/api/clients"),
            ("POST", "/api/clients/alice/revoke"),
            ("POST", "/api/crl/refresh"),
            ("POST", "/api/openvpn/restart"),
        ]
        peers = ("192.168.1.10", "10.20.0.10", "172.30.33.5", "172.30.32.1", "127.0.0.1", "::1")
        forged_headers = {
            "X-Forwarded-For": "172.30.32.2",
            "X-Real-IP": "172.30.32.2",
            "Forwarded": "for=172.30.32.2",
            "X-Ingress-Path": "/api/hassio_ingress/forged",
            "X-Remote-User-Name": "admin",
        }
        with patch("openvpn_addon.webui.collect_status") as status_action:
            for peer in peers:
                for method, path in routes:
                    with self.subTest(peer=peer, method=method, path=path):
                        status, body, _ = self.request(
                            path, {"client_name": "alice"} if method == "POST" else None,
                            method=method, peer=peer, headers=forged_headers,
                        )
                        self.assertEqual(403, status)
                        self.assertEqual("ingress only", json.loads(body)["message"])
            status_action.assert_not_called()
        for action in (
            self.actions.issue_client, self.actions.revoke_client, self.actions.export_profile,
            self.actions.refresh_crl, self.actions.restart_openvpn, self.actions.runtime_info,
        ):
            action.assert_not_called()

    def test_untrusted_peer_is_rejected_before_body_parsing(self):
        status, _, _ = self.request("/api/clients", method="POST", peer="192.168.1.10", raw_body=b"{nope")
        self.assertEqual(403, status)
        self.actions.issue_client.assert_not_called()

    def test_ingress_uses_socket_peer_despite_forwarded_lan_address(self):
        status, _, _ = self.request(
            "/api/clients", {"client_name": "alice"}, headers={"X-Forwarded-For": "192.168.1.10"},
        )
        self.assertEqual(200, status)
        self.actions.issue_client.assert_called_once_with("alice")


class PanelBindingTests(unittest.TestCase):
    """Only the explicitly identified Supervisor bridge may host the panel."""

    def setUp(self):
        discovery = patch("openvpn_addon.webui.network._host_ipv4_addresses")
        self.discovery = discovery.start()
        self.addCleanup(discovery.stop)

    def _interfaces(self, pairs):
        self.discovery.return_value = [
            (name, ipaddress.IPv4Interface(value)) for name, value in pairs
        ]

    def test_binds_only_supervisor_bridge_with_multiple_lan_interfaces(self):
        self._interfaces(
            [
                ("lo", "127.0.0.1/8"),
                ("enp1s0", "192.168.1.6/24"),
                ("enp2s0", "10.20.0.1/24"),
                ("hassio", "172.30.32.1/23"),
                ("docker0", "172.30.232.1/24"),
                ("dummy0", "169.254.10.1/16"),
            ]
        )
        # The secondary LAN is deliberately absent from the user's configuration.
        config = AddonConfig(local_networks=["192.168.1.0/24"])
        self.assertEqual(["172.30.32.1"], panel_bind_addresses(config))
        self.assertEqual(["172.30.32.1"], panel_bind_addresses(AddonConfig()))

    def test_unlisted_secondary_lan_is_not_trusted(self):
        self._interfaces([("enp1s0", "10.10.0.5/24"), ("br0", "10.20.0.1/24")])
        config = AddonConfig(local_networks=["10.10.0.0/24"])
        self.assertEqual([], panel_bind_addresses(config))

    def test_missing_bridge_disables_panel_instead_of_wildcard_fallback(self):
        self._interfaces([("lo", "127.0.0.1/8"), ("enp1s0", "192.168.1.6/24")])
        config = AddonConfig(local_networks=["192.168.1.0/24"])
        self.assertEqual([], panel_bind_addresses(config))

    def test_empty_discovery_disables_panel(self):
        self._interfaces([])
        self.assertEqual([], panel_bind_addresses(AddonConfig()))

    def test_discovery_error_disables_panel(self):
        for error in (FileNotFoundError("ip unavailable"), PermissionError("discovery denied")):
            with self.subTest(error=type(error).__name__):
                self.discovery.side_effect = error
                self.assertEqual([], panel_bind_addresses(AddonConfig()))

    def test_unrecognized_or_ambiguous_bridge_disables_panel(self):
        cases = [
            [("br0", "172.30.32.1/23")],
            [("hassio", "10.20.0.1/24")],
            [("hassio", "172.30.32.3/23")],
            [("hassio", "172.30.32.1/24")],
            [("hassio", "172.30.32.1/23"), ("hassio", "10.20.0.1/24")],
            [("hassio", "172.30.32.1/23"), ("enp2s0", "172.30.32.10/24")],
            [("hassio", "172.30.32.1/23"), ("hassio", "172.30.32.1/23")],
        ]
        for interfaces in cases:
            with self.subTest(interfaces=interfaces):
                self._interfaces(interfaces)
                self.assertEqual([], panel_bind_addresses(AddonConfig()))

    def test_default_route_is_not_a_trust_signal(self):
        self._interfaces([("hassio", "172.30.32.1/23")])
        with patch("openvpn_addon.webui.network._default_interface", side_effect=RuntimeError) as route:
            self.assertEqual(["172.30.32.1"], panel_bind_addresses(AddonConfig()))
            route.assert_not_called()


if __name__ == "__main__":
    unittest.main()
