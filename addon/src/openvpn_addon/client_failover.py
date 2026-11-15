"""Validated, ordered alternate addresses for exported client profiles."""

from __future__ import annotations

import ipaddress
import re

from .config import AddonConfig


_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def _endpoint(value: object, default_port: int) -> tuple[str, int]:
    # Accept only single OpenVPN tokens, never quoted text or raw directives.
    if not isinstance(value, str) or value.count(":") > 1:
        raise ValueError("Failover endpoints must be DNS names or IPv4 addresses with an optional port")
    host, separator, port_text = value.partition(":")
    if separator:
        if not re.fullmatch(r"[0-9]{1,5}", port_text) or not 1 <= int(port_text) <= 65535:
            raise ValueError("Failover endpoint port must be between 1 and 65535")
        port = int(port_text)
    else:
        port = default_port
    labels = host.removesuffix(".").split(".")
    if len(host) > 253 or not all(_HOST_LABEL.fullmatch(label) for label in labels):
        raise ValueError("Failover endpoint host must be a DNS name or IPv4 address")
    if all(char in "0123456789." for char in host):
        address = ipaddress.IPv4Address(host)
        if address.is_unspecified or address.is_multicast or int(address) == 0xFFFFFFFF:
            raise ValueError("Failover endpoint must be a unicast destination")
    return host, port


def client_failover_remotes(config: AddonConfig) -> list[tuple[str, int]]:
    """Return unique backups; validate the complete policy only when enabled."""
    if not config.experimental_client_failover:
        return []
    endpoints = config.client_fallback_endpoints
    if not isinstance(endpoints, list) or not 1 <= len(endpoints) <= 8:
        raise ValueError("client_fallback_endpoints must be a list of 1 to 8 endpoints")
    if type(config.client_connect_timeout) is not int or not 1 <= config.client_connect_timeout <= 300:
        raise ValueError("client_connect_timeout must be between 1 and 300 seconds")
    primary_host, primary_port = _endpoint(config.public_address, config.public_port)
    seen = {(primary_host.lower().removesuffix("."), primary_port)}
    remotes: list[tuple[str, int]] = []
    for value in endpoints:
        host, port = _endpoint(value, config.public_port)
        key = (host.lower().removesuffix("."), port)
        if key not in seen:
            seen.add(key)
            remotes.append((host, port))
    return remotes
