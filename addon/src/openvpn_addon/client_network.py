from __future__ import annotations

import ipaddress

from .config import AddonConfig
from .renderer import effective_push_networks


def parse_client_network(value: str) -> ipaddress.IPv4Network:
    """Require a mask so a LAN address is not accidentally checked as a /32."""
    if "/" not in value:
        raise ValueError("Client network requires an IPv4 address and prefix, e.g. 192.168.1.0/24")
    return ipaddress.IPv4Network(value, strict=False)


def check_client_networks(config: AddonConfig, networks: list[ipaddress.IPv4Network]) -> dict[str, object]:
    """Compare supplied client LANs with configured VPN destinations, without I/O."""
    destinations = [("vpn_subnet", ipaddress.IPv4Network(config.vpn_network))]
    destinations.extend(
        ("pushed_route", network)
        for network in effective_push_networks(config.push_routes, config.local_networks)
    )
    clients = list(dict.fromkeys(networks))
    conflicts = []
    for client in clients:
        for kind, destination in destinations:
            if not client.overlaps(destination):
                continue
            overlap = client if client.prefixlen >= destination.prefixlen else destination
            if kind == "vpn_subnet":
                risk = "Client LAN overlaps tunnel addresses; tunnel traffic may use the local LAN."
                recommendation = "Choose a VPN subnet that does not overlap client LANs."
            else:
                if client.prefixlen == destination.prefixlen:
                    risk = "Equal prefixes: route metrics and client OS behavior decide which network is reached."
                elif client.prefixlen > destination.prefixlen:
                    risk = "Client LAN is more specific; it can hide VPN destinations in the overlap."
                else:
                    risk = "Pushed route is more specific; it can divert traffic for local LAN devices into the VPN."
                recommendation = "Use non-overlapping client and remote LAN subnets, or plan explicit host routes."
            conflicts.append({
                "client_network": str(client),
                "kind": kind,
                "vpn_network": str(destination),
                "overlap": str(overlap),
                "risk": risk,
                "recommendation": recommendation,
            })

    return {
        "status": "warn" if conflicts else "ok",
        "source": "configured options (not live client routes)",
        "client_networks": [str(network) for network in clients],
        "checked_networks": [{"kind": kind, "network": str(network)} for kind, network in destinations],
        "conflicts": conflicts,
        "notes": [
            "Only the supplied IPv4 client LANs, VPN subnet and explicit pushed routes are compared. "
            "No overlap does not prove connectivity; client route filters, metrics and firewalls are not inspected.",
            "redirect_gateway does not resolve overlapping LAN addresses.",
        ],
    }
