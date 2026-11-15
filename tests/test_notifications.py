import io
import json
import socket
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request

from openvpn_addon import config as config_module, main, notifications, renderer
from openvpn_addon.config import AddonConfig, load_config
from openvpn_addon.validator import validate_config


ROOT = Path(__file__).resolve().parents[1]


def environment(event="client-connect"):
    return {
        "script_type": event,
        "common_name": "laptop",
        "ifconfig_pool_remote_ip": "10.8.0.2",
        "OPENVPN_NOTIFY_SOCKET": "/unused/notifications.sock",
        "private_key": "not-for-delivery",
        "password": "not-for-delivery",
        "SUPERVISOR_TOKEN": "not-for-delivery",
    }


class NotificationConfigTests(unittest.TestCase):
    def test_default_off_emits_no_hooks_or_worker(self):
        config = AddonConfig()
        self.assertFalse(config.experimental_client_notifications)
        with patch.object(renderer, "TEMPLATES_DIR", ROOT / "addon/templates"):
            rendered = renderer.render_server_config(config)
        for directive in ("script-security", "client-connect", "client-disconnect", "OPENVPN_NOTIFY"):
            self.assertNotIn(directive, rendered)
        with patch.object(notifications, "NotificationWorker") as worker:
            self.assertIsNone(notifications.start_notifications(config))
        worker.assert_not_called()

    def test_load_options_and_render_hooks_without_url(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "options.json"
            path.write_text(json.dumps({
                "experimental_client_notifications": "true",
                "client_notification_url": "https://receiver.invalid/private-hook",
            }))
            config = load_config(path)
        self.assertEqual(config.unknown_options, [])
        self.assertTrue(config.experimental_client_notifications)
        validate_config(config)
        with patch.object(renderer, "TEMPLATES_DIR", ROOT / "addon/templates"):
            rendered = renderer.render_server_config(config)
        self.assertIn("script-security 2", rendered)
        self.assertIn("client-connect /usr/local/bin/openvpn-notify", rendered)
        self.assertIn("client-disconnect /usr/local/bin/openvpn-notify", rendered)
        self.assertIn(str(config.notification_socket_path), rendered)
        self.assertNotIn("receiver.invalid", rendered)
        self.assertNotIn("private-hook", rendered)

    def test_url_validation_never_echoes_value(self):
        for url in ("", None, "file:///secret", "ftp://host/secret", "https://user:secret@host/",
                    "https://host/secret#fragment", "https://host:99999/secret",
                    "https://host:bad/secret", "https://[bad/secret", "https://host/secret\n",
                    "https:///secret", "https://host:0/secret"):
            with self.subTest(url_type=type(url).__name__):
                config = AddonConfig(experimental_client_notifications=True, client_notification_url=url)
                with self.assertRaises(ValueError) as raised:
                    validate_config(config)
                self.assertNotIn("secret", str(raised.exception))
        for url in ("http://homeassistant.local:8123/api/webhook/example", "https://host/hook?q=example"):
            validate_config(AddonConfig(experimental_client_notifications=True, client_notification_url=url))

    def test_disabled_url_is_inert(self):
        validate_config(AddonConfig(client_notification_url="not a URL"))

    def test_packaged_hook_has_fixed_python_and_import_path(self):
        script = (ROOT / "addon/rootfs/usr/local/bin/openvpn-notify").read_text()
        self.assertTrue(script.startswith("#!/usr/bin/python3\n"))
        self.assertIn('sys.path.insert(0, "/opt/openvpn_server/src")', script)
        self.assertIn("sys.exit(emit_event())", script)
        self.assertIn("/usr/local/bin/openvpn-notify", (ROOT / "addon/Dockerfile").read_text())


class NotificationHookTests(unittest.TestCase):
    def test_payload_allowlist_for_both_events(self):
        for kind in ("client-connect", "client-disconnect"):
            payload = notifications.event_payload(environment(kind))
            self.assertEqual(set(payload), notifications.EVENT_FIELDS)
            self.assertEqual(payload["event"], kind)
            self.assertEqual(payload["client_name"], "laptop")
            self.assertEqual(payload["vpn_ip"], "10.8.0.2")
            self.assertEqual(payload["schema_version"], 1)
            self.assertNotIn("not-for-delivery", json.dumps(payload))

    def test_missing_address_is_null(self):
        env = environment()
        del env["ifconfig_pool_remote_ip"]
        self.assertIsNone(notifications.event_payload(env)["vpn_ip"])

    def test_hook_only_enqueues_nonblocking_datagram(self):
        with patch.object(notifications.socket, "socket") as factory, patch.object(notifications, "deliver_event") as post:
            sender = factory.return_value.__enter__.return_value
            self.assertEqual(notifications.emit_event(environment()), 0)
        factory.assert_called_once_with(socket.AF_UNIX, socket.SOCK_DGRAM)
        sender.setblocking.assert_called_once_with(False)
        payload, address = sender.sendto.call_args.args
        self.assertEqual(address, "/unused/notifications.sock")
        self.assertEqual(json.loads(payload)["event"], "client-connect")
        post.assert_not_called()

    def test_hook_succeeds_when_unavailable_or_queue_full(self):
        for error in (FileNotFoundError(), BlockingIOError(), PermissionError()):
            with patch.object(notifications.socket, "socket") as factory:
                factory.return_value.__enter__.return_value.sendto.side_effect = error
                self.assertEqual(notifications.emit_event(environment()), 0)
        with patch.object(notifications.socket, "socket", side_effect=PermissionError()):
            self.assertEqual(notifications.emit_event(environment()), 0)

    def test_invalid_events_are_dropped_without_socket(self):
        for updates in ({"script_type": "up"}, {"common_name": ""}, {"common_name": "x" * 257},
                        {"ifconfig_pool_remote_ip": "invalid"}, {"common_name": "\U0001f600" * 256}):
            env = environment()
            env.update(updates)
            with patch.object(notifications.socket, "socket") as factory:
                self.assertEqual(notifications.emit_event(env), 0)
            factory.assert_not_called()


class NotificationDeliveryTests(unittest.TestCase):
    def test_post_json_with_timeout_and_without_proxies_or_redirects(self):
        with patch.object(notifications, "build_opener") as build:
            response = build.return_value.open.return_value.__enter__.return_value
            response.status = 204
            notifications.deliver_event("https://receiver.invalid/hook", b'{"event":"client-connect"}')
        handlers = build.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsInstance(handlers[1], notifications.NoRedirects)
        call = build.return_value.open.call_args
        request = call.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(request.data, b'{"event":"client-connect"}')
        self.assertEqual(call.kwargs["timeout"], 3.0)
        response.read.assert_not_called()

    def test_redirects_are_rejected(self):
        handler = notifications.NoRedirects()
        request = Request("https://receiver.invalid/private", data=b"{}")
        for code in (301, 302, 303, 307, 308):
            self.assertIsNone(handler.redirect_request(request, None, code, "", {}, "https://other.invalid"))

    def test_non_success_response_fails(self):
        with patch.object(notifications, "build_opener") as build:
            build.return_value.open.return_value.__enter__.return_value.status = 302
            with self.assertRaises(ValueError):
                notifications.deliver_event("https://receiver.invalid/hook", b"{}")

    def test_worker_survives_failures_without_logging_receiver_or_payload(self):
        worker = object.__new__(notifications.NotificationWorker)
        worker.stopping = threading.Event()
        worker.receiver = MagicMock()
        worker.url = "https://receiver.invalid/private-hook"
        payload = json.dumps(notifications.event_payload(environment())).encode()
        worker.receiver.recv.side_effect = [b"{", b"{}", b"x" * 2049, payload, payload, OSError()]
        output = io.StringIO()
        error = HTTPError(worker.url, 500, "private-response", {}, None)
        with patch.object(notifications, "deliver_event", side_effect=[error, None]) as deliver, redirect_stdout(output):
            worker.run()
        self.assertEqual(deliver.call_count, 2)
        self.assertIn("event dropped", output.getvalue())
        for text in ("private-hook", "private-response", "laptop", "receiver.invalid"):
            self.assertNotIn(text, output.getvalue())

    def test_worker_socket_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(config_module, "DATA_ROOT", Path(directory)), \
                patch.object(notifications.socket, "socket") as factory, patch.object(Path, "chmod") as chmod:
            config = AddonConfig(experimental_client_notifications=True)
            worker = notifications.NotificationWorker(config)
            factory.return_value.bind.assert_called_once_with(str(config.notification_socket_path))
            chmod.assert_called_once_with(0o600)
            with patch.object(worker, "join") as join:
                worker.stop()
            self.assertTrue(worker.stopping.is_set())
            factory.return_value.close.assert_called_once()
            join.assert_called_once_with(timeout=1)

    def test_failed_start_does_not_abort_vpn(self):
        with patch.object(notifications, "NotificationWorker", side_effect=OSError("private-url")), \
                redirect_stdout(io.StringIO()) as output:
            self.assertIsNone(notifications.start_notifications(AddonConfig(experimental_client_notifications=True)))
        self.assertNotIn("private-url", output.getvalue())

    def test_run_starts_worker_before_openvpn_and_stops_it(self):
        config = AddonConfig(experimental_client_notifications=True, client_notification_url="https://host/hook")
        with patch.multiple(main, load_and_validate=MagicMock(return_value=config), ensure_dirs=MagicMock(),
                            PkiManager=MagicMock(), sync_client_ips=MagicMock(), render_server_config=MagicMock(return_value=""),
                            log_routing_intent=MagicMock(), export_default_client=MagicMock(),
                            process_pending_requests=MagicMock(), apply_network=MagicMock(), remove_network=MagicMock(),
                            start_panel=MagicMock(return_value=[]), RequestWatcher=MagicMock()), \
                patch.object(Path, "write_text"), patch.object(main.signal, "signal"), \
                patch.object(main, "OpenVpnSupervisor") as supervisor, \
                patch.object(main, "start_notifications") as start:
            def supervise(_):
                start.assert_called_once_with(config)
            supervisor.return_value.supervise_forever.side_effect = supervise
            self.assertEqual(main.cmd_run(None), 0)
            start.return_value.stop.assert_called_once()

    def test_real_datagram_to_worker_smoke(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(config_module, "DATA_ROOT", Path(directory)):
            config = AddonConfig(experimental_client_notifications=True, client_notification_url="https://host/hook")
            config.runtime_dir.mkdir()
            try:
                worker = notifications.NotificationWorker(config)
            except PermissionError:
                self.skipTest("Sandbox denies Unix socket creation")
            received = threading.Event()
            with patch.object(notifications, "deliver_event", side_effect=lambda *_: received.set()) as deliver:
                worker.start()
                try:
                    self.assertEqual(config.notification_socket_path.stat().st_mode & 0o777, 0o600)
                    env = environment()
                    env["OPENVPN_NOTIFY_SOCKET"] = str(config.notification_socket_path)
                    self.assertEqual(notifications.emit_event(env), 0)
                    self.assertTrue(received.wait(2))
                    self.assertEqual(json.loads(deliver.call_args.args[1])["client_name"], "laptop")
                finally:
                    worker.stop()
            self.assertFalse(config.notification_socket_path.exists())


if __name__ == "__main__":
    unittest.main()
