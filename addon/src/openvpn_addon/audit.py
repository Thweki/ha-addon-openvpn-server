"""Bounded, private event history shared by the panel, CLI and OpenVPN hooks."""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from .config import AddonConfig
from .validator import CLIENT_NAME_RE


MAX_EVENTS = 1000
PAGE_SIZE = 100
EVENTS = {"client_connect", "client_disconnect", "client_revoke", "profile_download"}


def record_event(config: AddonConfig, event: str, client: str) -> bool:
    """Best effort: an unavailable audit store must not interrupt VPN operations.

    Only these three explicit fields reach disk; never pass profile bodies, request
    headers, environment dumps or exception messages into the history.
    """
    if not config.experimental_audit_log:
        return False
    try:
        if event not in EVENTS or not CLIENT_NAME_RE.fullmatch(client):
            raise ValueError("invalid audit event")
        path = config.audit_path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
        with closing(sqlite3.connect(path, timeout=0.25)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL, event TEXT NOT NULL, client TEXT NOT NULL
            )""")
            db.execute(
                "INSERT INTO events (timestamp, event, client) VALUES (?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), event, client),
            )
            db.execute(
                "DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT ?)",
                (MAX_EVENTS,),
            )
        return True
    except (OSError, sqlite3.Error, ValueError):
        # Do not expose certificate names, paths or raw exception data in logs.
        print("WARNING: audit event could not be recorded; history may be incomplete.", flush=True)
        return False


def read_events(config: AddonConfig, before: int | None = None) -> dict[str, Any]:
    """Return newest first, using a stable ID cursor while new events arrive."""
    result: dict[str, Any] = {
        "enabled": config.experimental_audit_log,
        "events": [],
        "next_before": None,
    }
    if not config.experimental_audit_log or not config.audit_path.exists():
        return result
    with closing(sqlite3.connect(config.audit_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.25)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT id, timestamp, event, client FROM events "
            "WHERE (? IS NULL OR id < ?) ORDER BY id DESC LIMIT ?",
            (before, before, PAGE_SIZE + 1),
        ).fetchall()
    result["events"] = [dict(row) for row in rows[:PAGE_SIZE]]
    if len(rows) > PAGE_SIZE:
        result["next_before"] = rows[PAGE_SIZE - 1]["id"]
    return result
