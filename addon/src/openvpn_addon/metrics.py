"""Aggregate read-only usage snapshots without exporting client identities."""

from __future__ import annotations

import os
import time
from typing import Iterable

from .config import AddonConfig


METRIC_HELP = {
    "status_readable": "Whether the OpenVPN status file could be read.",
    "status_valid": "Whether a complete, consistent status snapshot was parsed.",
    "status_fresh": "Whether a valid snapshot is within the freshness window.",
    "status_age_seconds": "Age of the status file modification time in seconds.",
    "connected_sessions": "Sessions in the current status snapshot, including shared certificates.",
    "connected_clients": "Distinct common names in the current status snapshot.",
    "session_received_bytes": "Bytes received by the server across currently connected sessions.",
    "session_sent_bytes": "Bytes sent by the server across currently connected sessions.",
}


def collect_metrics(config: AddonConfig) -> dict[str, int | float | None]:
    metrics: dict[str, int | float | None] = dict.fromkeys(METRIC_HELP)
    metrics.update(status_readable=0, status_valid=0, status_fresh=0)
    try:
        with config.status_path.open(encoding="utf-8") as stream:
            before = os.fstat(stream.fileno())
            usage = _parse_usage(stream)
            after = os.fstat(stream.fileno())
        current = config.status_path.stat()
    except (OSError, UnicodeError):
        return metrics

    metrics["status_readable"] = 1
    age = time.time() - after.st_mtime
    metrics["status_age_seconds"] = max(0.0, age)
    # OpenVPN rewrites the file in place. Reject a partial or changing snapshot.
    def signature(stat: os.stat_result) -> tuple[int, int, int]:
        return stat.st_ino, stat.st_size, stat.st_mtime_ns

    if usage is None or signature(before) != signature(after) or signature(after) != signature(current):
        return metrics

    metrics["status_valid"] = 1
    if 0 <= age <= max(60, 2 * config.status_interval):
        metrics["status_fresh"] = 1
        metrics.update(usage)
    return metrics


def _parse_usage(lines: Iterable[str]) -> dict[str, int] | None:
    """Read v1/v2/v3 sessions without collapsing duplicate common names.

    This is separate from the panel's per-client status view: aggregation must
    retain every session, and must reject partial snapshots rather than guess.
    """
    header: list[str] | None = None
    version = 0
    in_clients = False
    complete = False
    names: set[str] = set()
    sessions = received = sent = 0
    required = ("Common Name", "Bytes Received", "Bytes Sent")
    for line in lines:
        line = line.rstrip("\r\n")
        parts = line.split("\t" if "\t" in line else ",")
        if parts == ["END"]:
            complete = True
            break
        if parts[:2] == ["HEADER", "CLIENT_LIST"] or parts[0] == "Common Name":
            if header is not None:
                return None
            version = 2 if parts[0] == "HEADER" else 1
            header = parts[2:] if version == 2 else parts
            if any(header.count(key) != 1 for key in required):
                return None
            in_clients = True
            continue
        if version == 1 and parts == ["ROUTING TABLE"]:
            in_clients = False
            continue
        if version == 2 and parts[0] == "CLIENT_LIST":
            row = parts[1:]
        elif version == 1 and in_clients:
            row = parts
        elif parts[0] == "CLIENT_LIST":
            return None
        else:
            continue
        if header is None or len(row) != len(header):
            return None
        name, rx, tx = (row[header.index(key)] for key in required)
        if not name or not rx.isascii() or not rx.isdecimal() or not tx.isascii() or not tx.isdecimal():
            return None
        try:
            received += int(rx)
            sent += int(tx)
        except ValueError:
            return None
        names.add(name)
        sessions += 1
    if header is None or not complete:
        return None
    return {
        "connected_sessions": sessions,
        "connected_clients": len(names),
        "session_received_bytes": received,
        "session_sent_bytes": sent,
    }


def prometheus_metrics(metrics: dict[str, int | float | None]) -> str:
    lines: list[str] = []
    for key, help_text in METRIC_HELP.items():
        value = metrics[key]
        if value is None:
            continue
        name = f"openvpn_addon_{key}"
        lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} gauge", f"{name} {value}"))
    return "\n".join(lines) + "\n"
