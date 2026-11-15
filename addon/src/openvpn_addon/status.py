from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from .config import AddonConfig
from .health import collect_health, worst_status
from .network import ip_forward_enabled, verify_network
from .pki import PkiManager
from .renderer import effective_push_networks
from .validator import config_warnings


def collect_status(config: AddonConfig, pki: PkiManager | None = None) -> dict[str, object]:
    manager = pki or PkiManager(config)
    online = _parse_status(config.status_path)
    now = int(time.time())
    last_seen = _load_last_seen(config, now)
    fixed_ips = _load_fixed_ips(config)
    checks = collect_health(config, manager)
    networks = effective_push_networks(config.push_routes, config.local_networks)
    crl = manager.crl_info()

    clients: list[dict[str, object]] = []
    for client in manager.list_clients():
        session = online.get(client.name, {})
        clients.append(
            {
                "name": client.name,
                "status": client.status,
                "expires": client.expires,
                "vpn_address": fixed_ips.get(client.name) or session.get("virtual_address"),
                "online": client.name in online,
                "real_address": session.get("real_address"),
                "bytes_received": session.get("bytes_received"),
                "bytes_sent": session.get("bytes_sent"),
                "connected_since": session.get("connected_since"),
                "last_seen": last_seen.get(client.name),
                "inactive_days": (now - last_seen[client.name]) // 86400 if client.name in last_seen else None,
            }
        )

    network_result = _network_snapshot(config)

    return {
        "summary": {
            "listen_address": f"{config.server_host}:{config.listen_port}",
            "public_address": f"{config.public_address}:{config.public_port}",
            "protocol": config.protocol,
            "lan_access": bool(config.local_networks),
            "redirect_gateway": config.redirect_gateway,
            "nat_enabled": config.nat_enabled,
            "duplicate_cn": config.duplicate_cn,
            "fixed_client_ips": config.fixed_client_ips,
        },
        "routes": [str(network) for network in networks],
        "warnings": config_warnings(config),
        "health": {
            "status": worst_status(checks),
            "checks": [{"name": item.name, "status": item.status, "detail": item.detail} for item in checks],
        },
        "networking": {
            "vpn_network": config.vpn_network,
            "local_networks": list(config.local_networks),
            "nat_enabled": config.nat_enabled,
            "ip_forward": network_result["ip_forward"],
            "out_interface": network_result["out_interface"],
        },
        "crl": {
            "last_update": crl.last_update,
            "next_update": crl.next_update,
            "days_remaining": crl.days_remaining,
        },
        "clients": clients,
        "clients_online": list(online),
        "server_conf": str(config.server_conf_path),
        "status_file": str(config.status_path),
        "exports_dir": str(config.exports_dir),
        "share_exports_dir": str(config.share_exports_dir),
        "share_requests_dir": str(config.share_requests_dir),
        "share_results_dir": str(config.share_results_dir),
        "publish_profiles_to_share": config.publish_profiles_to_share,
    }


def status_json(config: AddonConfig, pki: PkiManager | None = None) -> str:
    return json.dumps(collect_status(config, pki), ensure_ascii=False, indent=2)


def record_client_activity(config: AddonConfig) -> None:
    """Called by the background watcher, even when nobody has the panel open.

    Use the snapshot's mtime, not the poll time: a stopped server can leave a
    status file behind. Keep only the latest observation per common name.
    """
    try:
        before = config.status_path.stat()
        online = _parse_status(config.status_path)
        after = config.status_path.stat()
    except FileNotFoundError:
        return
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        return  # OpenVPN rewrote the snapshot while we read it; retry next poll.
    now = int(time.time())
    observed_at = int(before.st_mtime)
    if not online or not 0 < observed_at <= now:
        return
    previous = _load_last_seen(config, now)
    updated = dict(previous)
    for name in online:
        updated[name] = max(previous.get(name, 0), observed_at)
    if updated == previous:
        return
    path = config.runtime_dir / "client_last_seen.json"
    # The watcher is the only writer. Atomic replacement keeps concurrent panel
    # and CLI readers from seeing partial JSON; mkstemp gives owner-only access.
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".client_last_seen-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(updated, stream, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _load_last_seen(config: AddonConfig, now: int) -> dict[str, int]:
    try:
        payload = json.loads((config.runtime_dir / "client_last_seen.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        name: timestamp for name, timestamp in payload.items()
        if type(timestamp) is int and 0 < timestamp <= now
    }


def _network_snapshot(config: AddonConfig) -> dict[str, object]:
    result = verify_network(config)
    return {
        "ip_forward": ip_forward_enabled(),
        "out_interface": result.out_interface,
    }


def _load_fixed_ips(config: AddonConfig) -> dict[str, str]:
    path = config.client_ips_path
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): str(value) for key, value in payload.items()}


def _parse_status(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    results: dict[str, dict[str, str]] = {}
    in_section = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("Common Name,"):
            in_section = True
            continue
        if in_section and line.startswith("ROUTING TABLE"):
            break
        if not in_section or not line:
            continue
        parts = line.split(",")
        if len(parts) < 5:
            continue
        results[parts[0]] = {
            "common_name": parts[0],
            "real_address": parts[1],
            "virtual_address": parts[2],
            "bytes_received": parts[3],
            "bytes_sent": parts[4],
            "connected_since": parts[5] if len(parts) > 5 else "",
        }
    return results
