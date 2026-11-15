from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable


class StatusHistory:
    """Bounded, process-local observations; never stores logs or profile contents."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._events: deque[dict[str, str]] = deque(maxlen=30)
        self._clients: set[str] = set()
        self._errors: set[str] = set()

    def record(self, kind: str, message: str) -> None:
        with self._lock:
            self._events.append({
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "kind": kind,
                "message": message[:256],
            })

    def snapshot(self) -> list[dict[str, str]]:
        with self._lock:
            return [dict(event) for event in reversed(self._events)]

    def collect(self, collector: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        # Serialize sampling across panel listeners so older requests cannot
        # overwrite a newer observation. Failed collections leave history alone.
        with self._lock:
            status = collector()
            clients = set(status["clients_online"])
            errors = {check["name"] for check in status["health"]["checks"] if check["status"] == "error"}
            for name in sorted(clients - self._clients):
                self.record("client", f"Client observed online: {name}")
            for name in sorted(self._clients - clients):
                self.record("client", f"Client observed offline: {name}")
            for name in sorted(errors - self._errors):
                self.record("error", f"Health check reports an error: {name}")
            for name in sorted(self._errors - errors):
                self.record("health", f"Health check no longer reports an error: {name}")
            self._clients, self._errors = clients, errors
            status["recent_status"] = self.snapshot()
            return status
