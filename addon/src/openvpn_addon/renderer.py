from __future__ import annotations

import ipaddress
from pathlib import Path
from string import Template

from .config import AddonConfig


TEMPLATES_DIR = Path("/opt/openvpn_server/templates")

TLS_VERSION_MIN = "1.2"
DEFAULT_DATA_CIPHERS = ["AES-256-GCM", "AES-128-GCM", "CHACHA20-POLY1305"]


def render_server_config(config: AddonConfig) -> str:
    template = Template((TEMPLATES_DIR / "server.conf.tpl").read_text(encoding="utf-8"))
    pushed_networks = effective_push_networks(config.push_routes, config.local_networks)
    return template.substitute(
        port=config.listen_port,
        protocol=config.protocol,
        device=config.device,
        vpn_subnet=config.vpn_subnet,
        vpn_netmask=config.vpn_netmask,
        server_cert=config.pki_dir / "issued" / "server.crt",
        server_key=config.pki_dir / "private" / "server.key",
        ca_cert=config.pki_dir / "ca.crt",
        crl=config.crl_path,
        tls_crypt_line=_tls_crypt_line(config),
        client_config_dir_line=_client_config_dir_line(config),
        status_path=config.status_path,
        keepalive=config.keepalive,
        data_ciphers=_data_ciphers(config),
        cipher=config.cipher,
        auth=config.auth,
        tls_version_min=TLS_VERSION_MIN,
        duplicate_cn=_bool_directive(config.duplicate_cn, "duplicate-cn"),
        client_to_client=_bool_directive(config.client_to_client, "client-to-client"),
        redirect_gateway=_redirect_gateway(config.redirect_gateway),
        push_dns=_push_dns(config.dns_servers),
        push_routes=_format_push_routes(pushed_networks),
        inactive_timeout=_inactive_timeout(config.inactive_timeout),
        status_interval=config.status_interval,
        verb=_verb_level(config.log_level),
        explicit_exit_notify=_explicit_exit_notify(config),
    )


def render_client_config(config: AddonConfig, client_name: str) -> str:
    template = Template((TEMPLATES_DIR / "client.ovpn.tpl").read_text(encoding="utf-8"))
    remote_host, remote_port = _resolve_remote(config)
    return template.substitute(
        remote_host=remote_host,
        remote_port=remote_port,
        protocol=config.protocol,
        data_ciphers=_data_ciphers(config),
        auth=config.auth,
        tls_version_min=TLS_VERSION_MIN,
        tls_crypt_block=_tls_crypt_block(config),
        ca=_read_file(config.pki_dir / "ca.crt"),
        cert=_read_cert_body(config.pki_dir / "issued" / f"{client_name}.crt"),
        key=_read_file(config.pki_dir / "private" / f"{client_name}.key"),
    )


def _resolve_remote(config: AddonConfig) -> tuple[str, int]:
    endpoint = config.public_address
    if ":" in endpoint:
        host, port = endpoint.rsplit(":", 1)
        return host, int(port)
    return endpoint, config.public_port


def _data_ciphers(config: AddonConfig) -> str:
    ciphers: list[str] = []
    for item in [config.cipher, *DEFAULT_DATA_CIPHERS]:
        name = item.strip()
        if name and name not in ciphers:
            ciphers.append(name)
    return ":".join(ciphers)


def _push_dns(items: list[str]) -> str:
    return "\n".join(f'push "dhcp-option DNS {item}"' for item in items)


def effective_push_networks(push_routes: list[str], local_networks: list[str]) -> list[ipaddress._BaseNetwork]:
    unique_networks: list[ipaddress._BaseNetwork] = []
    seen: set[str] = set()
    for item in [*local_networks, *push_routes]:
        network = ipaddress.ip_network(item, strict=False)
        key = str(network)
        if key in seen:
            continue
        seen.add(key)
        unique_networks.append(network)
    return unique_networks


def _format_push_routes(networks: list[ipaddress._BaseNetwork]) -> str:
    return "\n".join(
        f'push "route {network.network_address} {network.netmask}"'
        for network in networks
    )


def _tls_crypt_line(config: AddonConfig) -> str:
    return f"tls-crypt {config.tls_crypt_path}" if config.tls_crypt else ""


def _client_config_dir_line(config: AddonConfig) -> str:
    # A shared certificate cannot be pinned to one address, so ccd stays off in that mode.
    if not config.uses_client_config_dir:
        return ""
    return f"client-config-dir {config.ccd_dir}"


def _tls_crypt_block(config: AddonConfig) -> str:
    if not config.tls_crypt:
        return ""
    return "<tls-crypt>\n" + _read_file(config.tls_crypt_path) + "\n</tls-crypt>"


def _inactive_timeout(seconds: int) -> str:
    if seconds <= 0:
        return ""
    return f"inactive {seconds}"


def _explicit_exit_notify(config: AddonConfig) -> str:
    # OpenVPN ignores this for TCP and logs a notice about it.
    return "explicit-exit-notify 1" if config.protocol == "udp" else ""


def _redirect_gateway(enabled: bool) -> str:
    return 'push "redirect-gateway def1 bypass-dhcp"' if enabled else ""


def _bool_directive(enabled: bool, directive: str) -> str:
    return directive if enabled else ""


def _verb_level(level: str) -> int:
    mapping = {
        "trace": 7,
        "debug": 5,
        "info": 3,
        "notice": 3,
        "warning": 2,
        "error": 1,
        "fatal": 0,
    }
    return mapping[level]


def _read_cert_body(path: Path) -> str:
    content = _read_file(path)
    start_marker = "-----BEGIN CERTIFICATE-----"
    end_marker = "-----END CERTIFICATE-----"
    start = content.find(start_marker)
    end = content.find(end_marker)
    if start == -1 or end == -1:
        raise ValueError(f"Certificate PEM block not found in {path}")
    body = content[start + len(start_marker) : end]
    return body.strip()


def _read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()
