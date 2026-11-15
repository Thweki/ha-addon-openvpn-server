"""Read-only inspection of the signed CRL consumed by OpenVPN."""

from __future__ import annotations

import json
import re
import ssl
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def _timestamp(value: str) -> datetime:
    return datetime.fromtimestamp(ssl.cert_time_to_seconds(value), tz=timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def inspect_crl(pki_dir: Path, *, now: datetime | None = None) -> dict[str, object]:
    """Verify one CRL snapshot against ca.crt; never read keys or the CA index.

    Signature verification is not a CA trust-chain or runtime health check.
    Errors deliberately omit OpenSSL diagnostics and input contents.
    """
    checked_at = now if now is not None else datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        raise ValueError("CRL inspection time must have a timezone")
    try:
        pem = (pki_dir / "crl.pem").read_text(encoding="ascii")
    except FileNotFoundError:
        raise ValueError("CRL is missing; initialize the add-on first") from None
    except (OSError, UnicodeError):
        raise ValueError("Cannot read CRL") from None
    try:
        # stdin pins inspection and verification to the same bytes even if
        # EasyRSA replaces crl.pem during this command.
        result = subprocess.run(
            [
                "openssl", "crl", "-verify", "-CAfile", str(pki_dir / "ca.crt"),
                "-no-CApath", "-no-CAstore", "-noout", "-text",
                "-lastupdate", "-nextupdate",
            ],
            input=pem, capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        raise ValueError("OpenSSL is required to inspect the CRL") from None
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("OpenSSL could not inspect the CRL") from None
    # Some OpenSSL versions return zero even when signature verification fails.
    if result.returncode != 0 or "verify OK" not in result.stderr.splitlines():
        raise ValueError("Cannot verify CRL; check crl.pem and ca.crt")

    try:
        last_update = next(line[11:] for line in result.stdout.splitlines() if line.startswith("lastUpdate="))
        next_update = next(line[11:] for line in result.stdout.splitlines() if line.startswith("nextUpdate="))
        start, end = _timestamp(last_update), _timestamp(next_update)
        if end <= start:
            raise ValueError("invalid validity window")
        revoked = []
        in_revocations = False
        found_section = False
        pending_serial = None
        for line in result.stdout.splitlines():
            line = line.strip()
            if line == "No Revoked Certificates.":
                found_section = True
            elif line == "Revoked Certificates:":
                found_section = in_revocations = True
            elif in_revocations and line.startswith("Signature Algorithm:"):
                in_revocations = False
            elif in_revocations and line.startswith("Serial Number:"):
                if pending_serial is not None:
                    raise ValueError("missing revocation date")
                pending_serial = line.partition(":")[2].strip()
                if not re.fullmatch(r"[0-9A-Fa-f]+", pending_serial):
                    raise ValueError("invalid serial")
            elif in_revocations and line.startswith("Revocation Date:"):
                if pending_serial is None:
                    raise ValueError("missing serial")
                revoked.append({
                    "serial": pending_serial.upper(),
                    "revoked_at": _iso(_timestamp(line.partition(":")[2].strip())),
                })
                pending_serial = None
        if not found_section or pending_serial is not None:
            raise ValueError("incomplete revocation list")
    except (ValueError, OverflowError, StopIteration):
        raise ValueError("Cannot parse verified CRL metadata") from None

    status = "not_yet_valid" if checked_at < start else "expired" if checked_at >= end else "current"
    return {
        "schema_version": 1,
        "signature_verified": True,
        "status": status,
        "last_update": _iso(start),
        "next_update": _iso(end),
        "revoked_count": len(revoked),
        "revoked_certificates": sorted(revoked, key=lambda item: int(item["serial"], 16)),
    }


def render_crl(report: dict[str, object], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(report, indent=2)
    lines = [
        "CRL signature: verified against local ca.crt",
        f"Validity: {report['status']}",
        f"Last update: {report['last_update']}",
        f"Next update: {report['next_update']}",
        f"Revoked certificates: {report['revoked_count']}",
    ]
    for entry in report["revoked_certificates"]:
        lines.append(f"serial={entry['serial']}\trevoked_at={entry['revoked_at']}")
    return "\n".join(lines)
