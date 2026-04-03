from __future__ import annotations

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
    inactive_timeout: int = 0
    keepalive: str = "10 120"
    log_level: str = "info"
    status_interval: int = 30
    client_name: str = "homeassistant"
    client_expire_days: int = 825

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
    def ccd_dir(self) -> Path:
        return self.runtime_dir / "ccd"

    @property
    def public_address(self) -> str:
        return self.public_endpoint.strip() or self.server_host.strip()


def load_config(path: Path | None = None) -> AddonConfig:
    options_path = path or OPTIONS_PATH
    if options_path.exists():
        payload = json.loads(options_path.read_text(encoding="utf-8"))
    else:
        payload = {}

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
    return normalized


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
