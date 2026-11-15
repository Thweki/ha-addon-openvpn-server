from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from .config import AddonConfig
from .validator import validate_config


def backup_config(config: AddonConfig, destination: Path) -> None:
    """Export effective settings in Home Assistant options format, without PKI data."""
    validate_config(config)
    payload = asdict(config)
    payload.pop("unknown_options")
    for internal, option in (
        ("dns_servers", "dns"),
        ("local_networks", "lan_networks"),
        ("push_routes", "extra_routes"),
    ):
        payload[option] = ", ".join(payload.pop(internal))

    # Build a complete, owner-only file before publishing it. Hard-link creation
    # is atomic and fails if the destination exists, including dangling symlinks.
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=destination.parent, prefix=".openvpn-config-"
    ) as temporary:
        os.fchmod(temporary.fileno(), 0o600)
        json.dump(payload, temporary, indent=2, sort_keys=True)
        temporary.write("\n")
        temporary.flush()
        os.fsync(temporary.fileno())
        os.link(temporary.name, destination)
