from __future__ import annotations

import ipaddress
import re

from .config import AddonConfig


CLIENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
LOG_LEVELS = {"trace", "debug", "info", "notice", "warning", "error", "fatal"}
PROTOCOLS = {"udp", "tcp"}
# Below this many days the CRL has to be rebuilt by restarting the add-on.
CRL_REBUILD_WARNING_DAYS = 90


def validate_config(config: AddonConfig) -> None:
    if config.protocol not in PROTOCOLS:
        raise ValueError(f"Unsupported protocol: {config.protocol}")

    if not (1 <= config.listen_port <= 65535):
        raise ValueError(f"Invalid listen_port: {config.listen_port}")

    if not (1 <= config.public_port <= 65535):
        raise ValueError(f"Invalid public_port: {config.public_port}")

    ipaddress.IPv4Address(config.server_host)
    ipaddress.IPv4Address(config.vpn_subnet)
    ipaddress.IPv4Address(config.vpn_netmask)

    if config.public_endpoint:
        _validate_public_endpoint(config.public_endpoint)

    for item in config.dns_servers:
        ipaddress.IPv4Address(item)

    for item in config.push_routes:
        ipaddress.IPv4Network(item, strict=False)

    for item in config.local_networks:
        ipaddress.IPv4Network(item, strict=False)

    if config.log_level not in LOG_LEVELS:
        raise ValueError(f"Invalid log level: {config.log_level}")

    if config.inactive_timeout < 0:
        raise ValueError("inactive_timeout must be >= 0")

    validated_idle_policies(config)

    if config.status_interval <= 0:
        raise ValueError("status_interval must be > 0")

    if config.client_expire_days <= 0:
        raise ValueError("client_expire_days must be > 0")

    if config.crl_valid_days <= 0:
        raise ValueError("crl_valid_days must be > 0")

    validate_client_name(config.client_name)

    vpn_network = ipaddress.IPv4Network(config.vpn_network, strict=False)
    for item in config.local_networks:
        local_network = ipaddress.IPv4Network(item, strict=False)
        if vpn_network.overlaps(local_network):
            raise ValueError(
                f"VPN subnet {vpn_network} overlaps with local_networks entry {local_network}"
            )


def validated_idle_policies(config: AddonConfig) -> dict[str, tuple[int, int]]:
    """Return enabled client timers, validating before any runtime files are written."""
    if type(config.experimental_client_idle) is not bool:
        raise ValueError("experimental_client_idle must be a boolean")
    if not config.experimental_client_idle:
        return {}
    if not config.uses_client_config_dir:
        raise ValueError("experimental_client_idle requires fixed_client_ips=true and duplicate_cn=false")
    if not isinstance(config.client_idle_policies, list):
        raise ValueError("client_idle_policies must be a list")

    policies: dict[str, tuple[int, int]] = {}
    for entry in config.client_idle_policies:
        if not isinstance(entry, dict) or not {"client", "timeout"} <= entry.keys():
            raise ValueError("Each client_idle_policies entry requires client and timeout")
        if entry.keys() - {"client", "timeout", "min_bytes"}:
            raise ValueError("Unknown field in client_idle_policies")
        name = entry["client"]
        if not isinstance(name, str) or not CLIENT_NAME_RE.fullmatch(name) or name in {"DEFAULT", "server"}:
            raise ValueError("client_idle_policies requires a non-reserved client certificate name")
        if name in policies:
            raise ValueError("client_idle_policies contains a duplicate client")
        seconds = entry["timeout"]
        min_bytes = entry.get("min_bytes", 0)
        for value in (seconds, min_bytes):
            if type(value) is not int or not 0 <= value <= 2147483647:
                raise ValueError("Idle timeout and min_bytes must be integers between 0 and 2147483647")
        if seconds == 0 and min_bytes:
            raise ValueError("Idle min_bytes requires a positive timeout")
        policies[name] = (seconds, min_bytes)
    return policies


def validate_client_name(name: str) -> None:
    if not CLIENT_NAME_RE.match(name):
        raise ValueError(
            "Client name must start with an alphanumeric character and use only "
            "letters, digits, underscore, dash, or dot."
        )


def config_warnings(config: AddonConfig) -> list[str]:
    warnings: list[str] = []

    if config.experimental_client_idle:
        warnings.append(
            "EXPERIMENTAL: per-client idle policies are enabled; selected clients may be "
            "disconnected for inactivity or low traffic. Client apps may reconnect automatically."
        )

    for option in config.unknown_options:
        warnings.append(
            f"Unknown option '{option}' is set but not used by this version; remove it from the "
            "add-on configuration to silence this warning."
        )

    if not config.public_endpoint.strip():
        warnings.append(
            "public_endpoint is empty; client profile export will be skipped until a routable address or domain is set."
        )

    if not config.nat_enabled and config.local_networks:
        warnings.append(
            "nat_enabled is off but lan_networks is set; VPN clients will still receive routes to "
            f"{', '.join(config.local_networks)} but the add-on will not NAT or forward them, so "
            "clients can only reach the VPN server itself. Enable nat_enabled to reach your LAN."
        )

    if config.nat_enabled and not config.local_networks:
        warnings.append(
            "nat_enabled is on but local_networks is empty; VPN clients will not automatically receive routes to your home LAN."
        )

    if config.redirect_gateway:
        warnings.append(
            "redirect_gateway is enabled; all client internet traffic will be sent through the VPN."
        )

    if config.duplicate_cn:
        warnings.append(
            "duplicate_cn is enabled; multiple devices can share one certificate, which weakens client-level revocation."
        )
        if config.fixed_client_ips:
            warnings.append(
                "fixed_client_ips is enabled but duplicate_cn is on; one shared certificate cannot be "
                "pinned to a single VPN address, so per-client IP assignment is skipped. Disable "
                "duplicate_cn for one certificate per device."
            )

    if config.public_port != config.listen_port:
        warnings.append(
            "public_port differs from listen_port; ensure your router or firewall forwards the external port to the local listening port."
        )

    for item in config.local_networks:
        network = ipaddress.IPv4Network(item, strict=False)
        if network.prefixlen in (32, 0):
            warnings.append(
                f"lan_networks entry '{item}' is not a subnet; use the network address with a prefix, "
                "for example 192.168.1.0/24."
            )

    if config.crl_valid_days < CRL_REBUILD_WARNING_DAYS:
        warnings.append(
            f"crl_valid_days is only {config.crl_valid_days} day(s); the certificate revocation list is "
            "rebuilt at add-on startup, so the add-on must be restarted before it expires. A longer "
            "value is recommended."
        )

    return warnings


def _validate_public_endpoint(value: str) -> None:
    if ":" in value:
        host, port = value.rsplit(":", 1)
        if not host:
            raise ValueError("public_endpoint host is empty")
        if not port.isdigit() or not (1 <= int(port) <= 65535):
            raise ValueError("public_endpoint port is invalid")
        return

    if not value.strip():
        raise ValueError("public_endpoint is empty")
