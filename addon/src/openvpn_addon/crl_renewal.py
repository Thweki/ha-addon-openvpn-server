"""Opt-in automatic CRL maintenance; client certificates and keys are never renewed."""
from __future__ import annotations

import ssl
import subprocess
import time

from .config import AddonConfig
from .pki import PkiManager


CHECK_INTERVAL_SECONDS = 3600


def renew_crl_if_due(config: AddonConfig) -> bool:
    """Renew within seven days (or half the configured lifetime for short-lived CRLs)."""
    if config.experimental_crl_auto_renew is not True:
        raise ValueError("Experimental CRL auto-renewal is disabled")
    if config.crl_valid_days <= 0:
        raise ValueError("crl_valid_days must be > 0")

    pki = PkiManager(config)
    with pki.mutation_lock():
        # Never initialize/recover a PKI implicitly: a missing index could lose revocations.
        if not (config.pki_dir / "index.txt").is_file():
            raise ValueError("CRL auto-renewal requires an initialized PKI index")
        window = min(7 * 86400, config.crl_valid_days * 86400 / 2)
        if config.crl_path.exists():
            result = subprocess.run(
                ["openssl", "crl", "-in", str(config.crl_path), "-noout", "-nextupdate"],
                capture_output=True, text=True, check=True, timeout=30,
            )
            label, _, value = result.stdout.strip().partition("=")
            if label != "nextUpdate":
                raise ValueError("CRL has no nextUpdate")
            if ssl.cert_time_to_seconds(value) - time.time() > window:
                return False

        # EasyRSA gen-crl signs to a temporary file and moves it over crl.pem only
        # after signing succeeds. Suppress ALL child output, including on failure.
        pki._run_unlocked("gen-crl", cwd=config.pki_dir.parent, quiet=True)
        return True


class CrlRenewalSchedule:
    """Driven by the existing request watcher; failures retry on the next hourly check."""

    def __init__(self, config: AddonConfig) -> None:
        if config.experimental_crl_auto_renew is not True:
            raise ValueError("Experimental CRL auto-renewal is disabled")
        self.config = config
        self.next_check = 0.0

    def tick(self) -> None:
        now = time.monotonic()
        if now < self.next_check:
            return
        self.next_check = now + CHECK_INTERVAL_SECONDS
        try:
            if renew_crl_if_due(self.config):
                print("Experimental CRL auto-renewal: revocation list refreshed.", flush=True)
        except Exception:  # noqa: BLE001 - maintenance must not stop request processing
            # Exception text and subprocess output may contain sensitive material.
            print("WARNING: experimental CRL auto-renewal failed; check PKI/CRL health. "
                  "Retrying in one hour.", flush=True)
