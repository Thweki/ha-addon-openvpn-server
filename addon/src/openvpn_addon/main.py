from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass

from .config import DATA_ROOT, load_config
from .network import apply_network
from .pki import PkiManager
from .requests import archive_request, load_client_requests, write_result
from .renderer import effective_push_networks, render_client_config, render_server_config
from .status import status_json
from .validator import config_warnings, validate_client_name, validate_config


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openvpn-addon")
    subparsers = parser.add_subparsers(dest="command", required=True)

    _command(subparsers, "validate", cmd_validate)
    _command(subparsers, "init", cmd_init)
    _command(subparsers, "run", cmd_run)
    _command(subparsers, "status", cmd_status)

    client = subparsers.add_parser("client", help="Manage OpenVPN client certificates and exports")
    client_sub = client.add_subparsers(dest="client_command", required=True)

    create = client_sub.add_parser("create", help="Create a client certificate and publish its .ovpn profile")
    create.add_argument("name", nargs="?")
    create.set_defaults(func=cmd_client_create)

    issue = client_sub.add_parser("issue", help="Explicit alias for create+publish workflow")
    issue.add_argument("name", nargs="?")
    issue.set_defaults(func=cmd_client_create)

    revoke = client_sub.add_parser("revoke", help="Revoke an existing client certificate")
    revoke.add_argument("name")
    revoke.set_defaults(func=cmd_client_revoke)

    export = client_sub.add_parser("export", help="Re-render and publish a client .ovpn profile")
    export.add_argument("name")
    export.set_defaults(func=cmd_client_export)

    list_parser = client_sub.add_parser("list", help="List issued client certificates")
    list_parser.set_defaults(func=cmd_client_list)
    return parser


def _command(subparsers: argparse._SubParsersAction, name: str, func) -> None:
    parser = subparsers.add_parser(name)
    parser.set_defaults(func=func)


def cmd_validate(_: argparse.Namespace) -> int:
    config = load_and_validate()
    print(
        f"Config valid: listen={config.protocol}:{config.listen_port}, "
        f"public={config.public_address}:{config.public_port}",
        flush=True,
    )
    return 0


def cmd_init(_: argparse.Namespace) -> int:
    config = load_and_validate()
    ensure_dirs(config)
    pki = PkiManager(config)
    pki.ensure_base()
    pki.ensure_client(config.client_name)
    config.server_conf_path.write_text(render_server_config(config), encoding="utf-8")
    maybe_export_default_client(config)
    process_client_requests(config, pki)
    print(f"Initialized add-on state under {DATA_ROOT}", flush=True)
    return 0


def cmd_run(_: argparse.Namespace) -> int:
    config = load_and_validate()
    ensure_dirs(config)
    pki = PkiManager(config)
    pki.ensure_base()
    pki.ensure_client(config.client_name)
    config.server_conf_path.write_text(render_server_config(config), encoding="utf-8")
    log_routing_intent(config)
    maybe_export_default_client(config)
    process_client_requests(config, pki)
    apply_network(config)
    return subprocess.run(["openvpn", "--config", str(config.server_conf_path)], check=False).returncode


def cmd_status(_: argparse.Namespace) -> int:
    config = load_and_validate()
    print(status_json(config))
    return 0


def cmd_client_create(args: argparse.Namespace) -> int:
    config = load_and_validate()
    ensure_dirs(config)
    name = args.name or config.client_name
    validate_client_name(name)
    pki = PkiManager(config)
    pki.ensure_base()
    pki.ensure_client(name)
    export_result = export_client(config, name)
    print(f"Issued client certificate: {name}", flush=True)
    print(f"Internal export path: {export_result.export_path}", flush=True)
    print(f"Published profile path: {export_result.publish_path}", flush=True)
    return 0


def cmd_client_revoke(args: argparse.Namespace) -> int:
    config = load_and_validate()
    pki = PkiManager(config)
    pki.revoke_client(args.name)
    remove_published_profile(config, args.name)
    print(f"Revoked client: {args.name}", flush=True)
    return 0


def cmd_client_export(args: argparse.Namespace) -> int:
    config = load_and_validate()
    export_result = export_client(config, args.name)
    print(f"Re-exported client profile: {args.name}", flush=True)
    print(f"Internal export path: {export_result.export_path}", flush=True)
    print(f"Published profile path: {export_result.publish_path}", flush=True)
    return 0


def cmd_client_list(_: argparse.Namespace) -> int:
    config = load_and_validate()
    pki = PkiManager(config)
    for item in pki.list_clients():
        print(item, flush=True)
    return 0


class ExportResult:
    def __init__(self, export_path, publish_path) -> None:
        self.export_path = export_path
        self.publish_path = publish_path


def export_client(config, name: str) -> ExportResult:
    validate_client_name(name)
    if config.public_address == "0.0.0.0":
        raise ValueError("Set public_endpoint or a concrete server_host before exporting a client profile")
    output = render_client_config(config, name)
    path = config.exports_dir / f"{name}.ovpn"
    path.write_text(output, encoding="utf-8")
    publish_path = publish_export(config, path)
    print(f"Published client profile to {publish_path}", flush=True)
    return ExportResult(path, publish_path)


def load_and_validate():
    config = load_config()
    validate_config(config)
    return config


def ensure_dirs(config) -> None:
    for path in [
        DATA_ROOT,
        config.runtime_dir,
        config.exports_dir,
        config.logs_dir,
        config.clients_dir,
        config.ccd_dir,
        config.share_exports_dir,
        config.share_requests_dir,
        config.share_results_dir,
        config.share_archive_dir,
    ]:
        path.mkdir(parents=True, exist_ok=True)


def maybe_export_default_client(config) -> None:
    if config.public_address == "0.0.0.0":
        print(
            "Skipping default client export because public_endpoint/server_host is not routable yet.",
            flush=True,
        )
        return
    export_result = export_client(config, config.client_name)
    print(f"Exported default client profile: {export_result.export_path}", flush=True)


def publish_export(config, source_path):
    target_path = config.share_exports_dir / source_path.name
    shutil.copy2(source_path, target_path)
    return target_path


def remove_published_profile(config, client_name: str) -> tuple[object, object]:
    internal_path = config.exports_dir / f"{client_name}.ovpn"
    shared_path = config.share_exports_dir / f"{client_name}.ovpn"
    if internal_path.exists():
        internal_path.unlink()
    if shared_path.exists():
        shared_path.unlink()
    return internal_path, shared_path


def log_routing_intent(config) -> None:
    networks = effective_push_networks(config.push_routes, config.local_networks)
    print(
        f"OpenVPN will listen on {config.server_host}:{config.listen_port}; "
        f"exported clients will connect to {config.public_address}:{config.public_port}.",
        flush=True,
    )
    print(
        f"Configuration summary: protocol={config.protocol}, "
        f"lan_access={'enabled' if bool(config.local_networks) else 'disabled'}, "
        f"redirect_gateway={'enabled' if config.redirect_gateway else 'disabled'}.",
        flush=True,
    )
    for warning in config_warnings(config):
        print(f"Configuration warning: {warning}", flush=True)
    if not networks:
        print("No extra LAN routes will be pushed to VPN clients.", flush=True)
        return
    rendered = ", ".join(str(network) for network in networks)
    print(f"VPN clients will receive routes to: {rendered}", flush=True)


def process_client_requests(config, pki: PkiManager) -> None:
    requests = load_client_requests(config.share_requests_dir)
    if not requests:
        return

    print(f"Found {len(requests)} pending client request(s) in {config.share_requests_dir}", flush=True)
    for request in requests:
        print(f"Processing client {request.action} request: {request.request_path.name}", flush=True)
        result_payload = process_client_request(config, pki, request.action, request.client_name)
        result_path = write_result(config.share_results_dir, request.request_path.name, result_payload)
        archive_path = archive_request(config.share_archive_dir, request.request_path)
        print(f"Result file: {result_path}", flush=True)
        print(f"Archived request: {archive_path}", flush=True)


def process_client_request(config, pki: PkiManager, action: str, client_name: str) -> dict[str, str]:
    if action == "issue":
        pki.ensure_client(client_name)
        export_result = export_client(config, client_name)
        print(f"Issued client certificate: {client_name}", flush=True)
        print(f"Internal export path: {export_result.export_path}", flush=True)
        print(f"Published profile path: {export_result.publish_path}", flush=True)
        return {
            "status": "ok",
            "action": "issue",
            "client_name": client_name,
            "internal_export_path": str(export_result.export_path),
            "published_profile_path": str(export_result.publish_path),
        }

    if action == "revoke":
        pki.revoke_client(client_name)
        internal_path, shared_path = remove_published_profile(config, client_name)
        print(f"Revoked client certificate: {client_name}", flush=True)
        print(f"Removed internal export path: {internal_path}", flush=True)
        print(f"Removed published profile path: {shared_path}", flush=True)
        return {
            "status": "ok",
            "action": "revoke",
            "client_name": client_name,
            "internal_export_path": str(internal_path),
            "published_profile_path": str(shared_path),
        }

    raise ValueError(f"Unsupported request action: {action}")


if __name__ == "__main__":
    sys.exit(main())
