"""Best-effort connection webhooks, isolated from OpenVPN's synchronous hooks."""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import threading
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime, timezone
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .config import AddonConfig


MAX_EVENT_BYTES = 2048
HTTP_TIMEOUT = 3.0
EVENT_FIELDS = {"schema_version", "event", "client_name", "vpn_ip", "occurred_at"}


def event_payload(environment: Mapping[str, str]) -> dict[str, object]:
    event = environment.get("script_type", "")
    name = environment.get("common_name", "")
    if event not in {"client-connect", "client-disconnect"} or not name or len(name) > 256:
        raise ValueError("Unsupported notification event")
    address = environment.get("ifconfig_pool_remote_ip", "")
    return {
        "schema_version": 1,
        "event": event,
        "client_name": name,
        "vpn_ip": str(ipaddress.ip_address(address)) if address else None,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }


def emit_event(environment: Mapping[str, str] | None = None) -> int:
    """Always succeed; a missing worker or a full kernel queue simply drops the event."""
    try:
        environment = os.environ if environment is None else environment
        payload = json.dumps(event_payload(environment)).encode("utf-8")
        if len(payload) > MAX_EVENT_BYTES:
            return 0
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
            sender.setblocking(False)
            sender.sendto(payload, environment["OPENVPN_NOTIFY_SOCKET"])
    except Exception:  # The notification hook must never reject a VPN connection.
        pass
    return 0


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not send client identities or secret webhook paths to another endpoint.
        return None


def deliver_event(url: str, payload: bytes) -> None:
    # Ignore proxy environment variables; keep delivery to the configured receiver.
    opener = build_opener(ProxyHandler({}), NoRedirects())
    request = Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with opener.open(request, timeout=HTTP_TIMEOUT) as response:
        if not 200 <= response.status < 300:
            raise ValueError("Notification receiver did not accept the event")
        # Do not read or log the response body.


class NotificationWorker(threading.Thread):
    def __init__(self, config: AddonConfig) -> None:
        super().__init__(name="client-notifications", daemon=True)
        self.url = config.client_notification_url
        self.path = config.notification_socket_path
        self.stopping = threading.Event()
        self.receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            # Recover a stale socket after a crash, but never remove a regular file.
            if self.path.is_socket():
                self.path.unlink()
            self.receiver.bind(str(self.path))
            self.path.chmod(0o600)
            self.receiver.settimeout(0.25)
        except Exception:
            self.receiver.close()
            raise

    def run(self) -> None:
        while not self.stopping.is_set():
            try:
                payload = self.receiver.recv(MAX_EVENT_BYTES + 1)
            except TimeoutError:
                continue
            except OSError:
                return
            if self.stopping.is_set():
                return
            try:
                if len(payload) > MAX_EVENT_BYTES:
                    continue
                event = json.loads(payload)
                if not isinstance(event, dict) or set(event) != EVENT_FIELDS:
                    continue
                deliver_event(self.url, payload)
            except Exception:
                # URLs, HTTP exception text and response bodies can contain secrets.
                print("WARNING: client notification delivery failed; event dropped.", flush=True)

    def stop(self) -> None:
        self.stopping.set()
        self.receiver.close()
        self.join(timeout=1)
        with suppress(OSError):
            self.path.unlink(missing_ok=True)


def start_notifications(config: AddonConfig) -> NotificationWorker | None:
    if not config.experimental_client_notifications:
        return None
    worker = None
    try:
        worker = NotificationWorker(config)
        worker.start()
        return worker
    except Exception:
        if worker is not None:
            worker.receiver.close()
            with suppress(OSError):
                worker.path.unlink(missing_ok=True)
        print("WARNING: client notifications unavailable; VPN will continue without them.", flush=True)
        return None
