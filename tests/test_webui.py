import io
import json
from http.client import HTTPResponse
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import openvpn_addon.config as config_module
from openvpn_addon.config import AddonConfig
from openvpn_addon.audit import read_events, record_event
from openvpn_addon.validator import validate_client_name
from openvpn_addon.webui import PanelActions, PanelServer, _build_handler, _match_client_route


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


class MemorySocket:
    def __init__(self, data):
        self.input = io.BytesIO(data)
        self.output = bytearray()

    def makefile(self, mode, *args):
        return self.input

    def sendall(self, data):
        self.output.extend(data)


class AuditPanelTests(unittest.TestCase):
    """Exercise real HTTP parsing and responses without requiring a listening socket."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        patcher = patch.object(config_module, "DATA_ROOT", Path(self.tempdir.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = AddonConfig()

        def profile(name):
            if name == "ghost":
                raise ValueError("unknown client")
            return "client\nremote vpn.example.com 1194\n"

        self.actions = PanelActions(
            issue_client=lambda name: {}, revoke_client=lambda name: {},
            export_profile=profile, refresh_crl=lambda: {},
            restart_openvpn=lambda: {}, runtime_info=lambda: {},
        )

    def request(self, path):
        connection = MemorySocket(f"GET {path} HTTP/1.0\r\n\r\n".encode())
        handler = _build_handler(self.config, self.actions)
        with patch.object(handler, "log_message"):
            handler(connection, ("127.0.0.1", 1), None)
        with HTTPResponse(MemorySocket(bytes(connection.output))) as response:
            response.begin()
            return response.status, response.read().decode(), dict(response.headers)

    def test_audit_is_disabled_by_default(self):
        status, body, headers = self.request("/api/audit")
        self.assertEqual(200, status)
        self.assertFalse(json.loads(body)["enabled"])
        self.assertEqual("no-store", headers["Cache-Control"])
        self.request("/api/clients/alice/profile")
        self.assertFalse(self.config.audit_path.exists())

    def test_profile_download_records_only_success_and_no_profile_body(self):
        self.config.experimental_audit_log = True
        self.request("/api/clients/ghost/profile")
        self.assertEqual([], read_events(self.config)["events"])
        self.request("/api/clients/alice/profile")
        events = read_events(self.config)["events"]
        self.assertEqual(1, len(events))
        self.assertEqual("profile_download", events[0]["event"])
        self.assertEqual("alice", events[0]["client"])
        self.assertNotIn("remote vpn.example.com", json.dumps(events))

    def test_audit_pages_and_bad_cursors(self):
        self.config.experimental_audit_log = True
        for number in range(3):
            record_event(self.config, "client_connect", f"client{number}")
        with patch("openvpn_addon.audit.PAGE_SIZE", 2):
            status, body, _ = self.request("/api/audit")
            self.assertEqual(200, status)
            first = json.loads(body)
            self.assertEqual(["client2", "client1"], [e["client"] for e in first["events"]])
            status, body, _ = self.request(f"/api/audit?before={first['next_before']}")
            self.assertEqual(["client0"], [e["client"] for e in json.loads(body)["events"]])
            self.assertIsNone(json.loads(body)["next_before"])
        for cursor in ("0", "-1", "abc", "", str(2**64)):
            status, _, _ = self.request(f"/api/audit?before={cursor}")
            self.assertEqual(400, status)

    def test_audit_read_errors_are_explicit_and_redacted(self):
        self.config.experimental_audit_log = True
        self.config.audit_path.parent.mkdir()
        self.config.audit_path.write_text("invalid database")
        status, body, _ = self.request("/api/audit")
        self.assertEqual(503, status)
        self.assertEqual({"message": "Audit history unavailable"}, json.loads(body))

    def test_failed_profile_write_is_not_a_download(self):
        self.config.experimental_audit_log = True
        with patch.object(MemorySocket, "sendall", side_effect=BrokenPipeError):
            with self.assertRaises(BrokenPipeError):
                self.request("/api/clients/alice/profile")
        self.assertEqual([], read_events(self.config)["events"])

    def test_unavailable_audit_storage_does_not_block_profile_download(self):
        self.config.experimental_audit_log = True
        with patch("openvpn_addon.audit.os.open", side_effect=OSError):
            with patch("builtins.print"):
                status, body, _ = self.request("/api/clients/alice/profile")
        self.assertEqual(200, status)
        self.assertIn("remote vpn.example.com", body)

    def test_audit_page_uses_text_nodes_and_ingress_relative_urls(self):
        _, body, _ = self.request("/")
        self.assertIn("Audit history (experimental)", body)
        self.assertIn("cell.textContent = value", body)
        self.assertIn('api("api/audit"', body)


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
