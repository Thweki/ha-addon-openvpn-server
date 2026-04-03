from __future__ import annotations

import json
from ipaddress import ip_network
from pathlib import Path

from .config import AddonConfig
from .renderer import effective_push_networks
from .validator import config_warnings


def collect_status(config: AddonConfig) -> dict[str, object]:
    clients = []
    if config.status_path.exists():
        clients = _parse_status(config.status_path)
    networks = effective_push_networks(config.push_routes, config.local_networks)
    warnings = config_warnings(config)
    return {
        "summary": {
            "listen_address": f"{config.server_host}:{config.listen_port}",
            "public_address": f"{config.public_address}:{config.public_port}",
            "protocol": config.protocol,
            "lan_access": bool(config.local_networks),
            "redirect_gateway": config.redirect_gateway,
        },
        "routes": [str(network) for network in networks],
        "warnings": warnings,
        "networking": {
            "vpn_network": str(ip_network(f"{config.vpn_subnet}/{config.vpn_netmask}", strict=False)),
            "local_networks": list(config.local_networks),
            "nat_enabled": config.nat_enabled,
        },
        "server_conf": str(config.server_conf_path),
        "status_file": str(config.status_path),
        "clients_online": clients,
        "exports_dir": str(config.exports_dir),
        "share_exports_dir": str(config.share_exports_dir),
        "share_requests_dir": str(config.share_requests_dir),
        "share_results_dir": str(config.share_results_dir),
    }


def status_json(config: AddonConfig) -> str:
    return json.dumps(collect_status(config), ensure_ascii=False, indent=2)


def _parse_status(path: Path) -> list[dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    results: list[dict[str, str]] = []
    in_section = False
    for line in lines:
        if line.startswith("Common Name,"):
            in_section = True
            continue
        if in_section and line.startswith("ROUTING TABLE"):
            break
        if in_section and line:
            parts = line.split(",")
            if len(parts) >= 5:
                results.append(
                    {
                        "common_name": parts[0],
                        "real_address": parts[1],
                        "bytes_received": parts[2],
                        "bytes_sent": parts[3],
                        "connected_since": parts[4],
                    }
                )
    return results
