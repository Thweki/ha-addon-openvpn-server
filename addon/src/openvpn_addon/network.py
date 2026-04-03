from __future__ import annotations

import ipaddress
import subprocess
from pathlib import Path

from .config import AddonConfig


def apply_network(config: AddonConfig) -> None:
    _ensure_ip_forward()
    if not config.nat_enabled:
        print("NAT is disabled; skipping iptables rules.", flush=True)
        return

    default_interface = _default_interface()
    network = ipaddress.IPv4Network(f"{config.vpn_subnet}/{config.vpn_netmask}", strict=False)
    print(
        f"Applying NAT/forwarding rules for VPN network {network} via interface {default_interface}.",
        flush=True,
    )

    nat_rule_added = _ensure_iptables(
        ["-t", "nat", "-C", "POSTROUTING", "-s", str(network), "-o", default_interface, "-j", "MASQUERADE"],
        ["-t", "nat", "-A", "POSTROUTING", "-s", str(network), "-o", default_interface, "-j", "MASQUERADE"],
    )
    forward_in_added = _ensure_iptables(
        ["-C", "FORWARD", "-i", config.device + "0", "-j", "ACCEPT"],
        ["-A", "FORWARD", "-i", config.device + "0", "-j", "ACCEPT"],
    )
    forward_out_added = _ensure_iptables(
        ["-C", "FORWARD", "-o", config.device + "0", "-m", "state", "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
        ["-A", "FORWARD", "-o", config.device + "0", "-m", "state", "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
    )
    print(
        "NAT rule status: "
        f"postrouting={'added' if nat_rule_added else 'present'}, "
        f"forward_in={'added' if forward_in_added else 'present'}, "
        f"forward_out={'added' if forward_out_added else 'present'}.",
        flush=True,
    )


def _default_interface() -> str:
    result = subprocess.run(
        ["ip", "route", "show", "default"],
        capture_output=True,
        text=True,
        check=True,
    )
    fields = result.stdout.strip().split()
    if "dev" not in fields:
        raise RuntimeError("Unable to determine default network interface")
    return fields[fields.index("dev") + 1]


def _ensure_iptables(check_rule: list[str], add_rule: list[str]) -> bool:
    check = subprocess.run(["iptables", *check_rule], capture_output=True, text=True)
    if check.returncode == 0:
        return False
    subprocess.run(["iptables", *add_rule], check=True)
    return True


def _ensure_ip_forward() -> None:
    ip_forward_path = Path("/proc/sys/net/ipv4/ip_forward")
    if ip_forward_path.exists() and ip_forward_path.read_text(encoding="utf-8").strip() == "1":
        return

    command = ["sysctl", "-w", "net.ipv4.ip_forward=1"]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode == 0:
        return

    print(
        "Warning: unable to enable net.ipv4.ip_forward inside the add-on. "
        "Enable IPv4 forwarding on the Home Assistant host if routed VPN traffic is required."
        f" Host reports current value={ip_forward_path.read_text(encoding='utf-8').strip() if ip_forward_path.exists() else 'unknown'}.",
        flush=True,
    )
