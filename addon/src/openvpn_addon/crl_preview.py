"""Read-only simulation of the experimental auto-renewal policy proposed in PR #27."""
from __future__ import annotations

import ssl
import subprocess
from datetime import datetime, timedelta, timezone

from .config import AddonConfig


def preview_crl_renewal(config: AddonConfig, *, experimental: bool = False) -> dict[str, object]:
    """Plan one check; never initialize PKI, take a write lock, or invoke EasyRSA.

    A renewal decision predicts policy eligibility, not signing success. In particular,
    this deliberately does not read the CA key or validate the revocation database.
    """
    if experimental is not True:
        raise ValueError("Experimental CRL preview is disabled")

    now = datetime.now(timezone.utc)
    result: dict[str, object] = {
        "dry_run": True,
        "action": "blocked",
        "reason": "invalid_crl_valid_days",
        "checked_at": now.isoformat(),
        "renewal_window_seconds": None,
        "renewal_due_at": None,
        "proposed_next_update": None,
    }
    if type(config.crl_valid_days) is not int or config.crl_valid_days <= 0:
        return result
    try:
        lifetime = timedelta(days=config.crl_valid_days)
        proposed_expiry = (now + lifetime).isoformat()
    except OverflowError:
        return result
    window = min(timedelta(days=7), lifetime / 2)
    result["renewal_window_seconds"] = int(window.total_seconds())

    try:
        if not (config.pki_dir / "index.txt").is_file():
            result["reason"] = "missing_pki_index"
            return result
        try:
            config.crl_path.stat()
        except FileNotFoundError:
            result.update(action="renew", reason="missing_crl", proposed_next_update=proposed_expiry)
            return result
        completed = subprocess.run(
            ["openssl", "crl", "-in", str(config.crl_path), "-noout", "-nextupdate"],
            capture_output=True, text=True, check=True, timeout=30,
            stdin=subprocess.DEVNULL,
        )
        label, _, value = completed.stdout.strip().partition("=")
        if label != "nextUpdate":
            raise ValueError("Missing nextUpdate")
        expiry = datetime.fromtimestamp(ssl.cert_time_to_seconds(value), tz=timezone.utc)
        due = expiry - window
        result["renewal_due_at"] = due.isoformat()
        if now >= due:
            result.update(
                action="renew", reason="expired" if now >= expiry else "within_renewal_window",
                proposed_next_update=proposed_expiry,
            )
        else:
            result.update(action="keep", reason="outside_renewal_window")
    except (OSError, ValueError, OverflowError, subprocess.SubprocessError):
        # Neither child output nor exception text belongs in this public report.
        result["reason"] = "cannot_inspect_crl_or_index"
    return result
