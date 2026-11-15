"""Read-only certificate inventory from EasyRSA's public CA index metadata."""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .validator import validate_client_name


COLUMNS = ("name", "serial", "status", "expires_at", "revoked_at")


def _index_time(value: str) -> datetime:
    # OpenSSL uses UTCTime through 2049 and GeneralizedTime thereafter.
    if re.fullmatch(r"[0-9]{12}Z", value):
        century = "19" if int(value[:2]) >= 50 else "20"
        value = century + value
    if not re.fullmatch(r"[0-9]{14}Z", value):
        raise ValueError("invalid index timestamp")
    return datetime.strptime(value, "%Y%m%d%H%M%SZ").replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def collect_inventory(pki_dir: Path, *, now: datetime | None = None) -> dict[str, object]:
    """Return one row per indexed client certificate, including retained history.

    Only index.txt is read; certificates, keys, profiles and runtime state are
    never opened. Reject malformed records rather than publish a partial audit.
    """
    generated = now if now is not None else datetime.now(timezone.utc)
    if generated.tzinfo is None:
        raise ValueError("inventory time must have a timezone")
    generated = generated.astimezone(timezone.utc)
    try:
        lines = (pki_dir / "index.txt").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []

    certificates = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            state, expiry, revocation, serial, _, subject = line.split("\t")
            if not subject.startswith("/CN="):
                raise ValueError("unsupported subject")
            name = subject[4:]
            validate_client_name(name)
            if state not in {"V", "R", "E"} or not re.fullmatch(r"[0-9A-Fa-f]+", serial):
                raise ValueError("invalid certificate metadata")
            expires = _index_time(expiry)
            revoked = _index_time(revocation.split(",", 1)[0]) if state == "R" else None
            if state != "R" and revocation:
                raise ValueError("unexpected revocation date")
        except ValueError:
            # Never echo index contents into logs or error responses.
            raise ValueError(f"Invalid CA index record at line {number}") from None
        if name == "server":
            continue
        status = "revoked" if state == "R" else "expired" if state == "E" or expires <= generated else "valid"
        certificates.append({
            "name": name,
            "serial": serial.upper(),
            "status": status,
            "expires_at": _iso(expires),
            "revoked_at": _iso(revoked) if revoked else None,
        })

    return {
        "schema_version": 1,
        "generated_at": _iso(generated),
        "certificates": sorted(certificates, key=lambda item: (item["name"], item["serial"])),
    }


def render_inventory(inventory: dict[str, object], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(inventory, indent=2) + "\n"
    if output_format != "csv":
        raise ValueError("Unsupported inventory format")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(inventory["certificates"])
    return output.getvalue()
