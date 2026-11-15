import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from openvpn_addon import config, main, webui
from openvpn_addon.pki import PkiManager
from openvpn_addon.requests import scan_client_requests
from openvpn_addon.validator import validate_client_name


class ReservedClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.options = self.root / "options.json"
        self.options.write_text(json.dumps({"public_endpoint": "vpn.example.com"}))
        for target, attribute, value in (
            (config, "OPTIONS_PATH", self.options),
            (config, "DATA_ROOT", self.root / "data"),
            (config, "SHARE_ROOT", self.root / "share"),
            (main, "DATA_ROOT", self.root / "data"),
        ):
            self.enterContext(patch.object(target, attribute, value))
        self.config = config.load_config()

        # Keep the real client operations and validation; fail on any PKI access
        # or profile rendering before rejection, even if a caller catches it.
        self.ensure_base = self.enterContext(patch.object(
            PkiManager, "ensure_base", side_effect=AssertionError("unexpected PKI initialization")
        ))
        self.client_status = self.enterContext(patch.object(
            PkiManager, "client_status", side_effect=AssertionError("unexpected PKI read")
        ))
        self.pki_run = self.enterContext(patch.object(
            PkiManager, "_run", side_effect=AssertionError("unexpected PKI mutation")
        ))
        self.render = self.enterContext(patch.object(
            main, "render_client_config", side_effect=AssertionError("unexpected profile rendering")
        ))

    def assert_no_client_side_effects(self):
        for operation in (self.ensure_base, self.client_status, self.pki_run, self.render):
            operation.assert_not_called()
        self.assertFalse(self.config.pki_dir.exists())
        for directory in (self.config.exports_dir, self.config.share_exports_dir):
            self.assertEqual([], list(directory.glob("*.ovpn")))

    def run_cli(self, *arguments):
        with patch("sys.argv", ["openvpn-addon", *arguments]):
            return main.main()

    def test_reserved_name_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "reserved"):
            validate_client_name("server")

    def test_non_reserved_client_names_remain_valid(self):
        for name in ("homeassistant", "alice", "server-laptop", "my_server", "server.example"):
            with self.subTest(name=name):
                validate_client_name(name)

    def test_cli_rejects_server_for_every_named_client_operation(self):
        for operation in ("create", "issue", "revoke", "export"):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(ValueError, "reserved"):
                    self.run_cli("client", operation, "server")
                self.assert_no_client_side_effects()

    def test_default_client_configuration_is_rejected_before_initialization(self):
        self.options.write_text(json.dumps({"client_name": "server"}))
        for arguments in (("validate",), ("init",), ("run",), ("client", "create"), ("client", "issue")):
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(ValueError, "reserved"):
                    self.run_cli(*arguments)
                self.assert_no_client_side_effects()
        self.assertFalse(self.config.runtime_dir.exists())

    def test_pki_rejects_server_before_reading_or_changing_certificates(self):
        pki = PkiManager(self.config)
        for operation in (pki.ensure_client, pki.revoke_client):
            with self.subTest(operation=operation.__name__):
                with self.assertRaisesRegex(ValueError, "reserved"):
                    operation("server")
                self.assert_no_client_side_effects()

    def test_panel_rejects_issue_revoke_and_profile_download(self):
        # Capture the production callbacks without binding a listening socket.
        with patch.object(main, "PanelServer") as panel, patch.object(
            main, "panel_bind_addresses", return_value=["127.0.0.1"]
        ):
            panel.return_value.address = ("127.0.0.1", main.PANEL_PORT)
            main.start_panel(self.config, PkiManager(self.config), Mock(), threading.Event())
        actions = panel.call_args.args[1]
        handler = webui._build_handler(self.config, actions)
        for method, path, payload in (
            ("POST", "/api/clients", {"client_name": "server"}),
            ("POST", "/api/clients/server/revoke", {}),
            ("GET", "/api/clients/server/profile", None),
        ):
            with self.subTest(method=method, path=path):
                body = b"" if payload is None else json.dumps(payload).encode()
                request = (
                    f"{method} {path} HTTP/1.0\r\nContent-Length: {len(body)}\r\n\r\n".encode()
                    + body
                )
                response = io.BytesIO()
                connection = Mock()
                connection.makefile.return_value = io.BytesIO(request)
                connection.sendall.side_effect = response.write
                handler(connection, ("127.0.0.1", 12345), Mock())
                headers, result = response.getvalue().split(b"\r\n\r\n", 1)
                self.assertIn(b" 400 ", headers.split(b"\r\n")[0])
                self.assertEqual("error", json.loads(result)["status"])
                self.assertIn("reserved", json.loads(result)["message"])
                self.assert_no_client_side_effects()

    def test_request_files_are_rejected_and_quarantined(self):
        self.config.share_requests_dir.mkdir(parents=True)
        for name, payload in (
            ("default.json", {"client_name": "server"}),
            ("issue.json", {"action": "issue", "client_name": "server"}),
            ("revoke.json", {"action": "revoke", "client_name": "server"}),
        ):
            (self.config.share_requests_dir / name).write_text(json.dumps(payload))
        scan = scan_client_requests(self.config.share_requests_dir)
        self.assertEqual([], scan.accepted)
        self.assertEqual(3, len(scan.rejected))
        for rejection in scan.rejected:
            self.assertIn("reserved", rejection.reason)

        main.process_pending_requests(self.config)
        for rejection in scan.rejected:
            name = rejection.request_path.name
            result = self.config.share_results_dir / name.replace(".json", ".result.json")
            payload = json.loads(result.read_text())
            self.assertEqual("error", payload["status"])
            self.assertIn("reserved", payload["message"])
            self.assertTrue((self.config.share_rejected_dir / name).exists())
            self.assertFalse(rejection.request_path.exists())
        self.assertFalse(self.config.share_archive_dir.exists())
        self.assert_no_client_side_effects()


if __name__ == "__main__":
    unittest.main()
