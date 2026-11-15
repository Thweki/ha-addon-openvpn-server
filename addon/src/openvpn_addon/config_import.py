from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from .config import AddonConfig, _normalize_payload
from .validator import validate_config


# Deliberately explicit: adding a config field must not silently allow new data
# (especially credentials) into the settings-only snapshot format.
SNAPSHOT_TYPES = {
    **dict.fromkeys((
        "server_host", "public_endpoint", "protocol", "device", "vpn_subnet",
        "vpn_netmask", "dns", "extra_routes", "lan_networks", "cipher", "auth",
        "keepalive", "log_level", "client_name",
    ), str),
    **dict.fromkeys((
        "listen_port", "public_port", "crl_valid_days", "inactive_timeout",
        "status_interval", "client_expire_days",
    ), int),
    **dict.fromkeys((
        "nat_enabled", "redirect_gateway", "tls_crypt", "client_to_client",
        "duplicate_cn", "fixed_client_ips", "publish_profiles_to_share",
    ), bool),
}
MAX_SNAPSHOT_BYTES = 64 * 1024


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("Duplicate snapshot keys are not allowed")
        payload[key] = value
    return payload


def read_snapshot(source: Path) -> dict[str, object]:
    """Read a complete backup-config snapshot without coercing or dropping data."""
    with source.open("rb") as stream:
        data = stream.read(MAX_SNAPSHOT_BYTES + 1)
    if len(data) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Snapshot exceeds the size limit")
    payload = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(payload, dict) or payload.keys() != SNAPSHOT_TYPES.keys():
        raise ValueError("Snapshot must contain exactly the supported settings")
    for key, expected in SNAPSHOT_TYPES.items():
        value = payload[key]
        # bool is an int subclass, but is not a valid port or interval.
        if type(value) is not expected:
            raise ValueError("Snapshot setting has an invalid type")
        if isinstance(value, str) and (
            not value.isascii() or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or "-----BEGIN" in value or "<" in value or ">" in value
        ):
            raise ValueError("Snapshot contains unsupported setting text")

    # These values are interpolated directly into OpenVPN directives and are
    # not fully checked by the normal configuration validator.
    for key in ("cipher", "auth", "device"):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", payload[key]):
            raise ValueError("Snapshot contains an invalid directive value")
    if not re.fullmatch(r"[1-9][0-9]* +[1-9][0-9]*", payload["keepalive"]):
        raise ValueError("Snapshot contains an invalid keepalive setting")
    if payload["public_endpoint"] and not re.fullmatch(r"[A-Za-z0-9_.:-]+", payload["public_endpoint"]):
        raise ValueError("Snapshot contains an invalid endpoint")
    validate_config(AddonConfig(**_normalize_payload(payload)))
    return payload


def import_config(source: Path, destination: Path | None = None) -> None:
    """Validate a snapshot, optionally staging it as a new private options file."""
    payload = read_snapshot(source)
    if destination is None:
        return
    # Publish only a complete file, without overwriting even dangling symlinks.
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=destination.parent, prefix=".openvpn-import-"
    ) as temporary:
        os.fchmod(temporary.fileno(), 0o600)
        json.dump(payload, temporary, indent=2, sort_keys=True)
        temporary.write("\n")
        temporary.flush()
        os.fsync(temporary.fileno())
        os.link(temporary.name, destination)
