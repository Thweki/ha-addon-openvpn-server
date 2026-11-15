import argparse
import errno
import io
import json
import socket
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from openvpn_addon import config as config_module
from openvpn_addon import listen_port, main, renderer
from openvpn_addon.config import AddonConfig, load_config
from openvpn_addon.status import collect_status
from openvpn_addon.validator import config_warnings


ROOT = Path(__file__).resolve().parent.parent


class ListenPortTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(config_module, "DATA_ROOT", root))
        self.stack.enter_context(patch.object(config_module, "SHARE_ROOT", root / "share"))
        self.stack.enter_context(patch.object(main, "DATA_ROOT", root))
        self.stack.enter_context(patch.object(renderer, "TEMPLATES_DIR", ROOT / "addon/templates"))
        self.config = AddonConfig(experimental_random_port=True, public_endpoint="vpn.example.com", public_port=443)
        self.config.runtime_dir.mkdir()
        self.random = self.stack.enter_context(patch.object(listen_port.random, "SystemRandom"))
        self.sample = self.random.return_value.sample
        self.sample.return_value = [52001, 52002]
        self.socket = self.stack.enter_context(patch.object(listen_port.socket, "socket"))
        self.probe = self.socket.return_value.__enter__.return_value

    def test_default_off_does_not_probe_or_randomize(self):
        config = AddonConfig(listen_port=21194)
        self.assertFalse(config.experimental_random_port)
        self.assertEqual(21194, listen_port.select_listen_port(config))
        self.random.assert_not_called()
        self.socket.assert_not_called()
        self.assertFalse(any("randomization" in warning for warning in config_warnings(config)))

    def test_option_loading_and_manifest_default_off(self):
        path = self.config.runtime_dir / "options.json"
        for value, expected in [(False, False), (True, True), ("false", False), ("true", True)]:
            with self.subTest(value=value):
                path.write_text(json.dumps({"experimental_random_port": value}))
                self.assertIs(expected, load_config(path).experimental_random_port)
        path.write_text("{}")
        self.assertFalse(load_config(path).experimental_random_port)
        manifest = (ROOT / "addon/config.yaml").read_text()
        self.assertIn("  experimental_random_port: false\n", manifest)
        self.assertIn("  experimental_random_port: bool\n", manifest)
        for language in ("en", "zh-Hans"):
            translation = json.loads((ROOT / f"addon/translations/{language}.json").read_text())
            self.assertIn("experimental_random_port", translation["configuration"])

    def test_udp_and_tcp_probe_wildcard_and_close_socket(self):
        for protocol, kind in [("udp", socket.SOCK_DGRAM), ("tcp", socket.SOCK_STREAM)]:
            with self.subTest(protocol=protocol):
                self.socket.reset_mock()
                self.config.protocol = protocol
                self.assertEqual(52001, listen_port.select_listen_port(self.config))
                self.socket.assert_called_once_with(socket.AF_INET, kind)
                self.probe.bind.assert_called_once_with(("0.0.0.0", 52001))
                self.socket.return_value.__exit__.assert_called_once()
        self.sample.assert_called_with(range(49152, 65536), 64)

    def test_busy_candidate_is_skipped(self):
        self.probe.bind.side_effect = [OSError(errno.EADDRINUSE, "busy"), None]
        self.assertEqual(52002, listen_port.select_listen_port(self.config))
        self.assertEqual(2, self.socket.return_value.__exit__.call_count)

    def test_exhaustion_fails_without_falling_back_to_configured_port(self):
        self.sample.return_value = list(range(52000, 52064))
        self.probe.bind.side_effect = OSError(errno.EADDRINUSE, "busy")
        with self.assertRaisesRegex(RuntimeError, "64 probes"):
            listen_port.select_listen_port(self.config)
        self.assertEqual(64, self.probe.bind.call_count)
        self.assertEqual(1194, self.config.listen_port)

    def test_other_socket_errors_fail_immediately(self):
        self.probe.bind.side_effect = PermissionError(errno.EACCES, "denied")
        with self.assertRaises(PermissionError):
            listen_port.select_listen_port(self.config)
        self.assertEqual(1, self.probe.bind.call_count)
        self.socket.return_value.__exit__.assert_called_once()

    def test_recorded_port_is_read_without_probing(self):
        self.config.server_conf_path.write_text("port 52002\nproto udp\n")
        self.assertEqual(52002, listen_port.recorded_listen_port(self.config))
        self.socket.assert_not_called()
        self.random.assert_not_called()

    def test_disabled_ignores_previous_random_port(self):
        self.config.server_conf_path.write_text("port 52002\n")
        self.config.experimental_random_port = False
        self.assertEqual(1194, listen_port.recorded_listen_port(self.config))
        self.assertEqual(1194, listen_port.select_listen_port(self.config))

    def test_before_first_start_reports_configured_port(self):
        self.assertEqual(1194, listen_port.recorded_listen_port(self.config))

    def test_malformed_record_is_rejected(self):
        for content in ("port 0\n", "port 65536\n", "port invalid\n", "proto udp\n"):
            with self.subTest(content=content):
                self.config.server_conf_path.write_text(content)
                with self.assertRaises(ValueError):
                    listen_port.recorded_listen_port(self.config)

    def test_rendering_changes_listener_but_preserves_public_profile_port(self):
        self.config.listen_port = listen_port.select_listen_port(self.config)
        self.assertTrue(renderer.render_server_config(self.config).startswith("port 52001\n"))
        with patch.object(renderer, "_read_file", return_value="fixture"), patch.object(
            renderer, "_read_cert_body", return_value="fixture"
        ):
            profile = renderer.render_client_config(self.config, "demo")
            self.assertIn("remote vpn.example.com 443\n", profile)
            self.config.public_endpoint = "vpn.example.com:8443"
            self.assertIn("remote vpn.example.com 8443\n", renderer.render_client_config(self.config, "demo"))

    def test_status_and_panel_explain_random_port(self):
        self.config.listen_port = 52001
        summary = collect_status(self.config)["summary"]
        self.assertEqual("0.0.0.0:52001", summary["listen_address"])
        self.assertEqual("vpn.example.com:443", summary["public_address"])
        self.assertTrue(summary["experimental_random_port"])
        self.assertTrue(any("update router forwarding" in item for item in config_warnings(self.config)))
        from openvpn_addon.webui import PAGE, _build_handler
        self.assertIn("s.experimental_random_port", PAGE)
        self.assertIn("update router forwarding after each add-on start", PAGE)
        # Exercise the actual HTTP handler without opening a sandboxed socket.
        connection = Mock()
        connection.makefile.return_value = io.BytesIO(b"GET /api/status HTTP/1.0\r\n\r\n")
        actions = Mock()
        actions.runtime_info.return_value = {}
        _build_handler(self.config, actions)(connection, ("127.0.0.1", 0), Mock())
        response = b"".join(call.args[0] for call in connection.sendall.call_args_list)
        headers, body = response.split(b"\r\n\r\n", 1)
        self.assertIn(b"200 OK", headers)
        self.assertEqual(summary, json.loads(body)["summary"])

    def test_cli_status_uses_recorded_port(self):
        self.config.server_conf_path.write_text("port 52002\n")
        with patch.object(main, "load_and_validate", return_value=self.config), patch.object(
            main, "status_json", return_value="{}"
        ) as status, redirect_stdout(io.StringIO()):
            self.assertEqual(0, main.cmd_status(argparse.Namespace()))
        self.assertEqual(52002, status.call_args.args[0].listen_port)
        self.random.assert_not_called()

    def test_run_selects_once_and_passes_same_port_to_all_consumers(self):
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(main, "load_and_validate", return_value=self.config))
        mocked = {}
        for name in ("PkiManager", "sync_client_ips", "export_default_client", "process_pending_requests",
                     "apply_network", "remove_network", "OpenVpnSupervisor", "start_panel", "RequestWatcher"):
            mocked[name] = self.stack.enter_context(patch.object(main, name))
        mocked["apply_network"].return_value.warnings = []
        mocked["start_panel"].return_value = []
        self.stack.enter_context(patch.object(main.signal, "signal"))
        self.assertEqual(0, main.cmd_run(argparse.Namespace()))
        self.sample.assert_called_once()
        self.assertTrue(self.config.server_conf_path.read_text().startswith("port 52001\n"))
        for name in ("export_default_client", "process_pending_requests", "apply_network",
                     "OpenVpnSupervisor", "start_panel"):
            self.assertEqual(52001, mocked[name].call_args.args[0].listen_port, name)
        # A new add-on run makes a fresh selection even when server.conf exists.
        self.sample.return_value = [52002]
        self.assertEqual(0, main.cmd_run(argparse.Namespace()))
        self.assertEqual(2, self.sample.call_count)
        self.assertTrue(self.config.server_conf_path.read_text().startswith("port 52002\n"))

    def test_child_restart_never_selects_another_port(self):
        self.config.listen_port = 52001
        supervisor = main.OpenVpnSupervisor(self.config)
        with patch.object(supervisor, "stop"), patch.object(supervisor, "start"), redirect_stdout(io.StringIO()):
            supervisor.restart("manual request")
            supervisor.restart("certificate revoked")
        self.assertEqual(52001, supervisor.config.listen_port)
        self.random.assert_not_called()

    def test_validate_and_init_do_not_select_ports(self):
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(main, "load_and_validate", return_value=self.config))
        for name in ("PkiManager", "sync_client_ips", "export_default_client", "process_pending_requests"):
            self.stack.enter_context(patch.object(main, name))
        self.assertEqual(0, main.cmd_validate(argparse.Namespace()))
        self.assertEqual(0, main.cmd_init(argparse.Namespace()))
        self.random.assert_not_called()
        self.socket.assert_not_called()
        self.assertTrue(self.config.server_conf_path.read_text().startswith("port 1194\n"))

    def test_failed_selection_does_not_start_services_or_overwrite_record(self):
        self.config.server_conf_path.write_text("port 52000\n")
        self.stack.enter_context(patch.object(main, "load_and_validate", return_value=self.config))
        for name in ("PkiManager", "sync_client_ips"):
            self.stack.enter_context(patch.object(main, name))
        start = self.stack.enter_context(patch.object(main, "start_panel"))
        self.probe.bind.side_effect = PermissionError(errno.EACCES, "denied")
        with self.assertRaises(PermissionError):
            main.cmd_run(argparse.Namespace())
        start.assert_not_called()
        self.assertEqual("port 52000\n", self.config.server_conf_path.read_text())


if __name__ == "__main__":
    unittest.main()
