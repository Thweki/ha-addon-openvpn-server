from __future__ import annotations

import dataclasses
import ipaddress
import json
import os
from dataclasses import dataclass, field
from pathlib import Path


OPTIONS_PATH = Path(os.environ.get("OPENVPN_ADDON_OPTIONS", "/data/options.json"))
DATA_ROOT = Path(os.environ.get("OPENVPN_ADDON_DATA", "/data/openvpn"))
SHARE_ROOT = Path(os.environ.get("OPENVPN_ADDON_SHARE", "/share/openvpn_server"))


@dataclass(slots=True)
class AddonConfig:
    server_host: str = "0.0.0.0"
    public_endpoint: str = ""
    listen_port: int = 1194
    public_port: int = 1194
    protocol: str = "udp"
    device: str = "tun"
    vpn_subnet: str = "10.8.0.0"
    vpn_netmask: str = "255.255.255.0"
    dns_servers: list[str] = field(default_factory=lambda: ["1.1.1.1", "8.8.8.8"])
    push_routes: list[str] = field(default_factory=list)
    local_networks: list[str] = field(default_factory=list)
    nat_enabled: bool = True
    redirect_gateway: bool = False
    cipher: str = "AES-256-GCM"
    auth: str = "SHA256"
    tls_crypt: bool = True
    client_to_client: bool = False
    duplicate_cn: bool = False
    fixed_client_ips: bool = True
    publish_profiles_to_share: bool = False
    crl_valid_days: int = 3650
    inactive_timeout: int = 0
    keepalive: str = "10 120"
    log_level: str = "info"
    status_interval: int = 30
    client_name: str = "homeassistant"
    client_expire_days: int = 825
    # Populated by load_config(); never set through options.json in practice.
    unknown_options: list[str] = field(default_factory=list)

    @property
    def runtime_dir(self) -> Path:
        return DATA_ROOT / "runtime"

    @property
    def pki_dir(self) -> Path:
        return DATA_ROOT / "pki"

    @property
    def exports_dir(self) -> Path:
        return DATA_ROOT / "exports"

    @property
    def logs_dir(self) -> Path:
        return DATA_ROOT / "logs"

    @property
    def clients_dir(self) -> Path:
        return DATA_ROOT / "clients"

    @property
    def share_exports_dir(self) -> Path:
        return SHARE_ROOT / "clients"

    @property
    def share_requests_dir(self) -> Path:
        return SHARE_ROOT / "requests"

    @property
    def share_results_dir(self) -> Path:
        return SHARE_ROOT / "results"

    @property
    def share_archive_dir(self) -> Path:
        return SHARE_ROOT / "archive"

    @property
    def share_rejected_dir(self) -> Path:
        return SHARE_ROOT / "rejected"

    @property
    def server_conf_path(self) -> Path:
        return self.runtime_dir / "server.conf"

    @property
    def status_path(self) -> Path:
        return self.runtime_dir / "openvpn-status.log"

    @property
    def management_path(self) -> Path:
        return self.runtime_dir / "management.sock"

    @property
    def tls_crypt_path(self) -> Path:
        return self.pki_dir / "ta.key"

    @property
    def crl_path(self) -> Path:
        return self.pki_dir / "crl.pem"

    @property
    def ccd_dir(self) -> Path:
        return self.runtime_dir / "ccd"

    @property
    def client_ips_path(self) -> Path:
        return self.runtime_dir / "client_ips.json"

    @property
    def public_address(self) -> str:
        return self.public_endpoint.strip() or self.server_host.strip()

    @property
    def vpn_network(self) -> str:
        """Normalised CIDR form of the VPN subnet, e.g. 10.8.0.0/24."""
        return str(ipaddress.IPv4Network(f"{self.vpn_subnet}/{self.vpn_netmask}", strict=False))

    @property
    def uses_client_config_dir(self) -> bool:
        """ccd is only meaningful when every client has its own certificate."""
        return self.fixed_client_ips and not self.duplicate_cn


def load_config(path: Path | None = None) -> AddonConfig:
    options_path = path or OPTIONS_PATH
    if options_path.exists():
        payload = json.loads(options_path.read_text(encoding="utf-8"))
    else:
        payload = {}

    if not isinstance(payload, dict):
        raise ValueError(f"Add-on options must be a JSON object, got {type(payload).__name__}")

    return AddonConfig(**_normalize_payload(payload))


def _normalize_payload(payload: dict[str, object]) -> dict[str, object]:
    normalized = dict(payload)
    normalized["dns_servers"] = _parse_list_option(
        normalized.pop("dns", None),
        fallback=normalized.pop("dns_servers", None),
        default=["1.1.1.1", "8.8.8.8"],
    )
    normalized["local_networks"] = _parse_list_option(
        normalized.pop("lan_networks", None),
        fallback=normalized.pop("local_networks", None),
        default=[],
    )
    normalized["push_routes"] = _parse_list_option(
        normalized.pop("extra_routes", None),
        fallback=normalized.pop("push_routes", None),
        default=[],
    )

    known = {item.name for item in dataclasses.fields(AddonConfig)}
    unknown = sorted(key for key in normalized if key not in known)
    for key in unknown:
        normalized.pop(key, None)
    normalized["unknown_options"] = unknown

    return _coerce_scalars(normalized)


def _coerce_scalars(payload: dict[str, object]) -> dict[str, object]:
    """Coerce hand-edited options.json values to the declared field types."""
    result: dict[str, object] = {}
    for item in dataclasses.fields(AddonConfig):
        if item.name not in payload:
            continue
        value = payload[item.name]
        annotation = item.type if isinstance(item.type, str) else getattr(item.type, "__name__", "")
        if annotation == "bool":
            result[item.name] = _coerce_bool(value)
        elif annotation == "int":
            result[item.name] = _coerce_int(value)
        else:
            result[item.name] = value
    return result


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "yes", "on", "1"}:
            return True
        if text in {"false", "no", "off", "0", ""}:
            return False
    return bool(value)


def _coerce_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdigit():
            return int(text)
    raise ValueError(f"Expected an integer option, got {value!r}")


def _parse_list_option(
    preferred: object,
    *,
    fallback: object,
    default: list[str],
) -> list[str]:
    if preferred is not None:
        return _coerce_to_list(preferred)
    if fallback is not None:
        return _coerce_to_list(fallback)
    return list(default)


def _coerce_to_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        items: list[str] = []
        for chunk in value.replace("\n", ",").split(","):
            item = chunk.strip()
            if item:
                items.append(item)
        return items
    if value is None:
        return []
    item = str(value).strip()
    return [item] if item else []
