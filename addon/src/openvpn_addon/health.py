from __future__ import annotations

import math
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import AddonConfig
from .network import NetworkResult, verify_network
from .pki import ClientInfo, PkiManager, STATUS_VALID
from .validator import config_warnings


OK = "ok"
WARN = "warn"
ERROR = "error"

_ORDER = {ERROR: 0, WARN: 1, OK: 2}
CRL_WARN_DAYS = 90
CLIENT_EXPIRY_WARN_DAYS = 30


@dataclass(slots=True)
class Check:
    name: str
    status: str
    detail: str


def collect_health(
    config: AddonConfig,
    pki: PkiManager | None = None,
    *,
    clients: list[ClientInfo] | None = None,
) -> list[Check]:
    checks: list[Check] = []

    network = verify_network(config)
    checks.extend(_network_checks(config, network))

    manager = pki or PkiManager(config)
    checks.append(_crl_check(config, manager))
    checks.extend(_client_expiry_checks(manager.list_clients() if clients is None else clients))

    if config.public_endpoint.strip():
        checks.append(Check("public_endpoint", OK, f"clients connect to {config.public_address}:{config.public_port}"))
    else:
        checks.append(
            Check(
                "public_endpoint",
                WARN,
                "not set; client profiles cannot be exported until a public address or domain is configured",
            )
        )

    if config.public_port != config.listen_port:
        checks.append(
            Check(
                "port_mapping",
                OK,
                f"local {config.listen_port} is published as {config.public_port}; the router must forward "
                "the external port to the local one",
            )
        )
    else:
        checks.append(Check("port_mapping", OK, f"local and public port are both {config.listen_port}"))

    if config.protocol == "tcp":
        checks.append(
            Check(
                "transport",
                WARN,
                "TCP transport is in use; UDP is faster and avoids TCP-over-TCP retransmission stalls. "
                "Keep TCP only if your network blocks UDP.",
            )
        )

    for warning in config_warnings(config):
        checks.append(Check("configuration", WARN, warning))

    return checks


def _client_expiry_checks(clients: list[ClientInfo]) -> list[Check]:
    checks: list[Check] = []
    now = datetime.now(timezone.utc).timestamp()
    guidance = "Issue a replacement under a new name, install its profile, then revoke the old client."
    for client in clients:
        if client.status != STATUS_VALID:
            continue
        try:
            remaining = ssl.cert_time_to_seconds(client.expires or "") - now
        except (ValueError, OverflowError):
            checks.append(Check(
                "client_expiry", WARN,
                f"{client.name}: certificate expiry is unknown; inspect the client certificate.",
            ))
            continue
        if remaining <= 0:
            checks.append(Check("client_expiry", ERROR, f"{client.name}: certificate has expired. {guidance}"))
        elif remaining <= CLIENT_EXPIRY_WARN_DAYS * 86400:
            days = math.ceil(remaining / 86400)
            checks.append(Check(
                "client_expiry", WARN,
                f"{client.name}: certificate expires in {days} day(s). {guidance}",
            ))
    return checks


def worst_status(checks: list[Check]) -> str:
    if not checks:
        return OK
    return sorted(checks, key=lambda item: _ORDER.get(item.status, 1))[0].status


def _network_checks(config: AddonConfig, network: NetworkResult) -> list[Check]:
    checks: list[Check] = []

    if not config.nat_enabled:
        checks.append(
            Check(
                "lan_forwarding",
                WARN,
                "nat_enabled is off; VPN clients can reach the VPN server only, not the rest of the LAN",
            )
        )
    elif network.errors:
        checks.append(Check("lan_forwarding", ERROR, "; ".join(network.errors)))
    elif network.missing_rules:
        checks.append(Check("lan_forwarding", ERROR, "missing rules: " + ", ".join(network.missing_rules)))
    else:
        detail = "NAT and forwarding rules are in place"
        if network.out_interface:
            detail += f" (egress interface {network.out_interface})"
        checks.append(Check("lan_forwarding", OK, detail))

    checks.append(
        Check(
            "ip_forward",
            OK if network.ip_forward else ERROR,
            "net.ipv4.ip_forward is enabled"
            if network.ip_forward
            else "net.ipv4.ip_forward is disabled; the host must forward IPv4 for LAN access",
        )
    )

    if config.local_networks:
        checks.append(Check("lan_networks", OK, "pushed to clients: " + ", ".join(config.local_networks)))
    else:
        checks.append(Check("lan_networks", WARN, "no LAN networks configured; clients get no LAN routes"))

    return checks


def _crl_check(config: AddonConfig, pki: PkiManager) -> Check:
    info = pki.crl_info()
    if info.next_update is None:
        return Check("crl", ERROR, "certificate revocation list is missing or unreadable")

    detail = f"next update {info.next_update}"
    if info.days_remaining is None:
        return Check("crl", WARN, detail)
    detail += f" ({info.days_remaining} day(s) left)"

    if info.days_remaining <= 0:
        return Check("crl", ERROR, f"expired: {detail}. Restart the add-on to rebuild it.")
    if info.days_remaining < CRL_WARN_DAYS:
        return Check("crl", WARN, f"{detail}; restart the add-on to rebuild it")
    return Check("crl", OK, detail)
