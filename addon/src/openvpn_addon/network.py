from __future__ import annotations

import ipaddress
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import AddonConfig


CHAIN = "OPENVPN_ADDON"
IP_FORWARD_PATH = Path("/proc/sys/net/ipv4/ip_forward")


@dataclass(slots=True)
class NetworkResult:
    applied: bool = False
    ip_forward: bool = False
    out_interface: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    missing_rules: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.applied and self.ip_forward and not self.errors and not self.missing_rules


def apply_network(config: AddonConfig) -> NetworkResult:
    """Reconcile forwarding/NAT rules. Never raises: the caller keeps the service running."""
    result = NetworkResult()
    result.ip_forward = _ensure_ip_forward(result)

    if not config.nat_enabled:
        result.warnings.append(
            "nat_enabled is off; no NAT/forwarding rules were applied, so VPN clients can only reach "
            "the VPN server itself. Enable nat_enabled to reach your LAN."
        )
        print(f"WARNING: {result.warnings[-1]}", flush=True)
        return result

    if not shutil.which("iptables"):
        result.errors.append("iptables is not available inside the add-on container")
        print(f"ERROR: {result.errors[-1]}", flush=True)
        return result

    vpn_network = ipaddress.IPv4Network(config.vpn_network, strict=False)
    out_interface = _lan_interface(config, result)
    result.out_interface = out_interface

    print(
        f"Applying NAT/forwarding rules for VPN network {vpn_network}"
        + (f" via interface {out_interface}." if out_interface else " (interface undetermined)."),
        flush=True,
    )

    _ensure_chain("nat")
    _ensure_chain("filter")

    # Only our own chain is flushed, so rules added by the user or other add-ons are untouched.
    _iptables("-t", "nat", "-F", CHAIN)
    _iptables("-F", CHAIN)

    if out_interface:
        _add(result, ["-t", "nat", "-A", CHAIN, "-s", str(vpn_network), "-o", out_interface, "-j", "MASQUERADE"])
    else:
        _add(result, ["-t", "nat", "-A", CHAIN, "-s", str(vpn_network), "-j", "MASQUERADE"])

    _add(result, ["-A", CHAIN, "-i", f"{config.device}0", "-s", str(vpn_network), "-j", "ACCEPT"])
    _add_stateful_return_rule(result, config)

    _ensure_jump(result, "nat", "POSTROUTING")
    _ensure_jump(result, "filter", "FORWARD")

    result.applied = not result.errors
    _print_status(result)
    return result


def remove_network(config: AddonConfig) -> None:
    """Best-effort cleanup so a stopped add-on leaves no stale rules behind."""
    if not shutil.which("iptables"):
        return
    for table, parent in (("nat", "POSTROUTING"), ("filter", "FORWARD")):
        while _iptables("-t", table, "-C", parent, "-j", CHAIN).returncode == 0:
            if _iptables("-t", table, "-D", parent, "-j", CHAIN).returncode != 0:
                break
    for table in ("nat", "filter"):
        _iptables("-t", table, "-F", CHAIN)
        _iptables("-t", table, "-X", CHAIN)
    print("Removed add-on NAT/forwarding rules.", flush=True)


def verify_network(config: AddonConfig) -> NetworkResult:
    """Read-only check of the current networking state, used by status and the panel."""
    result = NetworkResult()
    result.ip_forward = ip_forward_enabled()
    result.out_interface = _lan_interface(config, NetworkResult())
    if not config.nat_enabled:
        result.warnings.append("nat_enabled is off; LAN access through the VPN is disabled.")
        return result
    if not shutil.which("iptables"):
        result.errors.append("iptables is not available inside the add-on container")
        return result

    vpn_network = ipaddress.IPv4Network(config.vpn_network, strict=False)
    if _iptables("-t", "nat", "-C", "POSTROUTING", "-j", CHAIN).returncode != 0:
        result.missing_rules.append("nat POSTROUTING jump to " + CHAIN)
    if result.out_interface:
        masquerade = ["-t", "nat", "-C", CHAIN, "-s", str(vpn_network), "-o", result.out_interface, "-j", "MASQUERADE"]
    else:
        masquerade = ["-t", "nat", "-C", CHAIN, "-s", str(vpn_network), "-j", "MASQUERADE"]
    if _iptables(*masquerade).returncode != 0:
        result.missing_rules.append("MASQUERADE rule")
    if _iptables("-C", "FORWARD", "-j", CHAIN).returncode != 0:
        result.missing_rules.append("filter FORWARD jump to " + CHAIN)
    if _iptables("-C", CHAIN, "-i", f"{config.device}0", "-s", str(vpn_network), "-j", "ACCEPT").returncode != 0:
        result.missing_rules.append(f"{config.device}0 inbound ACCEPT rule")

    result.applied = not result.missing_rules
    return result


def ip_forward_enabled() -> bool:
    if not IP_FORWARD_PATH.exists():
        return False
    return IP_FORWARD_PATH.read_text(encoding="utf-8").strip() == "1"


def _ensure_ip_forward(result: NetworkResult) -> bool:
    if ip_forward_enabled():
        return True

    command = ["sysctl", "-w", "net.ipv4.ip_forward=1"]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode == 0 and ip_forward_enabled():
        print("Enabled net.ipv4.ip_forward.", flush=True)
        return True

    result.errors.append(
        "unable to enable net.ipv4.ip_forward inside the add-on; enable IPv4 forwarding on the "
        "Home Assistant host or routed VPN traffic will not work"
    )
    print(f"ERROR: {result.errors[-1]}", flush=True)
    return False


def _add_stateful_return_rule(result: NetworkResult, config: AddonConfig) -> None:
    modern = ["-A", CHAIN, "-o", f"{config.device}0", "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"]
    completed = subprocess.run(["iptables", *modern], capture_output=True, text=True)
    if completed.returncode == 0:
        return
    legacy = ["-A", CHAIN, "-o", f"{config.device}0", "-m", "state", "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT"]
    completed = subprocess.run(["iptables", *legacy], capture_output=True, text=True)
    if completed.returncode != 0:
        _record(result, legacy, completed)


def _lan_interface(config: AddonConfig, result: NetworkResult) -> str | None:
    for item in config.local_networks:
        try:
            network = ipaddress.IPv4Network(item, strict=False)
        except ValueError:
            continue
        match = _interface_owning(network)
        if match:
            return match

    try:
        return _default_interface()
    except RuntimeError as error:
        result.warnings.append(str(error))
        return None


def _interface_owning(network: ipaddress.IPv4Network) -> str | None:
    for name, address in _host_ipv4_addresses():
        if address.ip in network:
            return name
    return None


def _host_ipv4_addresses() -> list[tuple[str, ipaddress.IPv4Interface]]:
    completed = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True)
    if completed.returncode != 0:
        return []
    found: list[tuple[str, ipaddress.IPv4Interface]] = []
    for line in completed.stdout.splitlines():
        fields = line.split()
        if "inet" not in fields:
            continue
        try:
            index = fields.index("inet")
            name = fields[1].rstrip(":")
            address = ipaddress.IPv4Interface(fields[index + 1])
        except (IndexError, ValueError):
            continue
        found.append((name, address))
    return found


def _default_interface() -> str:
    result = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("unable to read the default route inside the add-on")
    fields = result.stdout.strip().split()
    if "dev" not in fields:
        raise RuntimeError("unable to determine the default network interface")
    return fields[fields.index("dev") + 1]


def _ensure_chain(table: str) -> None:
    if _iptables("-t", table, "-L", CHAIN).returncode != 0:
        _iptables("-t", table, "-N", CHAIN)


def _ensure_jump(result: NetworkResult, table: str, parent: str) -> None:
    if _iptables("-t", table, "-C", parent, "-j", CHAIN).returncode == 0:
        return
    completed = _iptables("-t", table, "-A", parent, "-j", CHAIN)
    if completed.returncode != 0:
        _record(result, ["-t", table, "-A", parent, "-j", CHAIN], completed)


def _add(result: NetworkResult, rule: list[str]) -> None:
    completed = subprocess.run(["iptables", *rule], capture_output=True, text=True)
    if completed.returncode != 0:
        _record(result, rule, completed)


def _iptables(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["iptables", *args], capture_output=True, text=True)


def _record(result: NetworkResult, rule: list[str], completed: subprocess.CompletedProcess[str]) -> None:
    detail = (completed.stderr or completed.stdout or "").strip().splitlines()
    message = f"iptables {' '.join(rule)} failed: {detail[0] if detail else completed.returncode}"
    result.errors.append(message)
    print(f"ERROR: {message}", flush=True)


def _print_status(result: NetworkResult) -> None:
    if result.errors:
        print(f"NAT rule status: FAILED ({len(result.errors)} error(s)).", flush=True)
        return
    print(
        "NAT rule status: chain reconciled, "
        f"ip_forward={'on' if result.ip_forward else 'off'}, "
        f"out_interface={result.out_interface or 'unknown'}.",
        flush=True,
    )
