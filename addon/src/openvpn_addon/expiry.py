"""Read-only validity report for the two certificates shared by every VPN client."""

from __future__ import annotations

import ssl
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def collect_expiry(pki_dir: Path, within_days: int = 30, *, now: datetime | None = None) -> dict[str, Any]:
    if within_days < 0:
        raise ValueError("within_days must be non-negative")
    now = now if now is not None else datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone")
    now = now.astimezone(timezone.utc)
    certificates = [
        _certificate("ca", pki_dir / "ca.crt", now, within_days),
        _certificate("server", pki_dir / "issued" / "server.crt", now, within_days),
    ]
    states = {item["status"] for item in certificates}
    status = "error" if states - {"ok", "expiring"} else "warning" if "expiring" in states else "ok"
    # An unreadable certificate leaves the shared deadline unknown, even if the
    # other certificate was decoded successfully.
    deadline = None
    limiting = []
    if all(item["not_after"] is not None for item in certificates):
        deadline = min(item["not_after"] for item in certificates)
        limiting = [item["role"] for item in certificates if item["not_after"] == deadline]
    return {
        "checked_at": now.isoformat(),
        "within_days": within_days,
        "status": status,
        "expiry_deadline": deadline,
        "limiting_certificates": limiting,
        "certificates": certificates,
    }


def _certificate(role: str, path: Path, now: datetime, within_days: int) -> dict[str, Any]:
    item: dict[str, Any] = {
        "role": role,
        "status": "unreadable",
        "not_before": None,
        "not_after": None,
        "days_remaining": None,
    }
    try:
        # Never follow a certificate symlink into a private key or profile.
        if path.is_symlink():
            return item
        if not path.exists():
            item["status"] = "missing"
            return item
        if not path.is_file():
            return item
        completed = subprocess.run(
            ["openssl", "x509", "-in", str(path), "-noout", "-startdate", "-enddate"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if completed.returncode != 0:
            return item
        dates = dict(line.partition("=")[::2] for line in completed.stdout.splitlines())
        start, end = (
            datetime.fromtimestamp(ssl.cert_time_to_seconds(dates[key]), tz=timezone.utc)
            for key in ("notBefore", "notAfter")
        )
        if end <= start:
            return item
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, OverflowError):
        # Do not echo subprocess output, exception messages, or certificate data.
        return item

    remaining = end - now
    item.update(not_before=start.isoformat(), not_after=end.isoformat(), days_remaining=remaining.days)
    if end <= now:
        item["status"] = "expired"
    elif start > now:
        item["status"] = "not_yet_valid"
    elif remaining.total_seconds() <= within_days * 86400:
        item["status"] = "expiring"
    else:
        item["status"] = "ok"
    return item


def expiry_text(report: dict[str, Any]) -> str:
    lines = [f"CA/server certificate expiry: {report['status']} (window: {report['within_days']} days)"]
    for item in report["certificates"]:
        lines.append(
            f"{item['role']}: {item['status']} | valid from {item['not_before'] or 'unknown'}"
            f" | expires {item['not_after'] or 'unknown'} | whole days remaining: {item['days_remaining']}"
        )
    limiting = ", ".join(report["limiting_certificates"])
    lines.append(f"Shared expiry deadline: {report['expiry_deadline'] or 'unknown'}" + (f" ({limiting})" if limiting else ""))
    return "\n".join(lines)
