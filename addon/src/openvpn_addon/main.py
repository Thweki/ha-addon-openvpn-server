from __future__ import annotations

import argparse
import collections
import json
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import DATA_ROOT, AddonConfig, load_config
from .health import collect_health, worst_status
from .network import apply_network, ip_forward_enabled, remove_network
from .pki import PkiManager
from .renderer import effective_push_networks, render_client_config, render_server_config
from .requests import archive_request, reject_request, scan_client_requests, write_result
from .status import collect_status, status_json
from .validator import config_warnings, validate_client_name, validate_config
from .webui import PanelActions, PanelServer, panel_bind_addresses


PANEL_PORT = 8099
REQUEST_POLL_SECONDS = 5.0
OPENVPN_FAST_EXIT_SECONDS = 10.0


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

    export = client_sub.add_parser("export", help="Re-render a client .ovpn profile")
    export.add_argument("name")
    export.set_defaults(func=cmd_client_export)

    list_parser = client_sub.add_parser("list", help="List issued client certificates")
    list_parser.set_defaults(func=cmd_client_list)
    return parser


def _command(subparsers: argparse._SubParsersAction, name: str, func: Callable[[argparse.Namespace], int]) -> None:
    parser = subparsers.add_parser(name)
    parser.set_defaults(func=func)


def cmd_validate(_: argparse.Namespace) -> int:
    config = load_and_validate()
    print(
        f"Config valid: listen={config.protocol}:{config.listen_port}, "
        f"public={config.public_address}:{config.public_port}",
        flush=True,
    )
    for warning in config_warnings(config):
        print(f"Configuration warning: {warning}", flush=True)
    return 0


def cmd_init(_: argparse.Namespace) -> int:
    config = load_and_validate()
    ensure_dirs(config)
    pki = PkiManager(config)
    pki.ensure_base()
    pki.ensure_client(config.client_name)
    sync_client_ips(config, pki)
    config.server_conf_path.write_text(render_server_config(config), encoding="utf-8")
    export_default_client(config)
    process_pending_requests(config)
    print(f"Initialized add-on state under {DATA_ROOT}", flush=True)
    return 0


def cmd_run(_: argparse.Namespace) -> int:
    config = load_and_validate()
    ensure_dirs(config)

    pki = PkiManager(config)
    pki.ensure_base()
    pki.ensure_client(config.client_name)
    sync_client_ips(config, pki)
    config.server_conf_path.write_text(render_server_config(config), encoding="utf-8")
    log_routing_intent(config)
    export_default_client(config)
    process_pending_requests(config)

    network = apply_network(config)
    for warning in network.warnings:
        print(f"Network warning: {warning}", flush=True)

    supervisor = OpenVpnSupervisor(config)
    stop_event = threading.Event()

    panel = start_panel(config, pki, supervisor, stop_event)
    watcher = RequestWatcher(config, stop_event)

    def handle_signal(signum: int, _frame: Any) -> None:
        print(f"Received signal {signum}; shutting down.", flush=True)
        stop_event.set()
        supervisor.stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        supervisor.supervise_forever(stop_event)
    finally:
        watcher.stop()
        for server in panel:
            server.stop()
        supervisor.stop()
        remove_network(config)
    return 0


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
    result = issue_client(config, pki, name)
    print(f"Issued client certificate: {name} ({result['action']})", flush=True)
    print(f"Internal export path: {result['export_path']}", flush=True)
    if result.get("publish_path"):
        print(f"Published profile path: {result['publish_path']}", flush=True)
    else:
        print("Profile is available from the add-on panel (publish_profiles_to_share is disabled).", flush=True)
    return 0


def cmd_client_revoke(args: argparse.Namespace) -> int:
    config = load_and_validate()
    pki = PkiManager(config)
    pki.revoke_client(args.name)
    sync_client_ips(config, pki)
    print(f"Revoked client: {args.name}", flush=True)
    print("Restart the add-on (or use the panel) so OpenVPN reloads the revocation list.", flush=True)
    return 0


def cmd_client_export(args: argparse.Namespace) -> int:
    config = load_and_validate()
    pki = PkiManager(config)
    validate_client_name(args.name)
    result = export_client(config, args.name)
    print(f"Re-exported client profile: {args.name}", flush=True)
    print(f"Internal export path: {result.export_path}", flush=True)
    return 0


def cmd_client_list(_: argparse.Namespace) -> int:
    config = load_and_validate()
    pki = PkiManager(config)
    clients = pki.list_clients()
    if not clients:
        print("(no clients issued)", flush=True)
    for client in clients:
        print(f"{client.name}\t{client.status}\texpires={client.expires or 'unknown'}", flush=True)
    return 0


@dataclass(slots=True)
class ExportResult:
    export_path: Path
    publish_path: Path | None = None


class OpenVpnSupervisor:
    """Runs OpenVPN as a supervised child process with bounded restart backoff."""

    def __init__(self, config: AddonConfig) -> None:
        self.config = config
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.RLock()
        self._log: collections.deque[str] = collections.deque(maxlen=600)
        self._started_at: float | None = None
        self.restarts = 0
        self.last_exit_code: int | None = None

    def start(self) -> None:
        with self._lock:
            if self.is_running():
                return
            self._process = subprocess.Popen(
                ["openvpn", "--config", str(self.config.server_conf_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            self._started_at = time.time()
            threading.Thread(target=self._pump, args=(self._process,), daemon=True).start()

    def _pump(self, process: subprocess.Popen[str]) -> None:
        stream = process.stdout
        if stream is None:
            return
        for line in stream:
            text = line.rstrip("\n")
            self._log.append(text)
            print(text, flush=True)

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def uptime_seconds(self) -> float | None:
        if not self.is_running() or self._started_at is None:
            return None
        return time.time() - self._started_at

    def restart(self, reason: str) -> dict[str, Any]:
        print(f"Restarting OpenVPN: {reason}", flush=True)
        self.stop()
        self.start()
        return {"status": "ok", "message": f"OpenVPN restarted ({reason})", "pid": self.pid}

    def stop(self, timeout: float = 10.0) -> None:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                return
            process.terminate()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=timeout)
            finally:
                self._process = None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def log_tail(self, lines: int = 60) -> list[str]:
        return list(self._log)[-lines:]

    def supervise_forever(self, stop_event: threading.Event) -> None:
        backoff = 1.0
        while not stop_event.is_set():
            self.start()
            process = self._process
            if process is None:
                return
            exit_code = process.wait()
            if stop_event.is_set():
                return

            self.last_exit_code = exit_code
            self.restarts += 1
            uptime = time.time() - (self._started_at or time.time())
            if uptime < OPENVPN_FAST_EXIT_SECONDS:
                backoff = min(backoff * 2, 60.0)
                print(
                    f"ERROR: OpenVPN exited with code {exit_code} after {uptime:.0f}s. The add-on panel stays "
                    f"available with diagnostics; retrying in {backoff:.0f}s.",
                    flush=True,
                )
            else:
                backoff = 1.0
                print(f"OpenVPN exited with code {exit_code}; restarting in {backoff:.0f}s.", flush=True)
            stop_event.wait(backoff)


class RequestWatcher(threading.Thread):
    """Applies /share request files without restarting the add-on."""

    def __init__(self, config: AddonConfig, stop_event: threading.Event) -> None:
        super().__init__(name="request-watcher", daemon=True)
        self.config = config
        self.stop_event = stop_event
        self.start()

    def run(self) -> None:
        while not self.stop_event.wait(REQUEST_POLL_SECONDS):
            try:
                process_pending_requests(self.config)
            except Exception as error:  # noqa: BLE001 - the watcher must never die
                print(f"ERROR: request watcher failed: {error}", flush=True)

    def stop(self) -> None:
        self.stop_event.set()
        self.join(timeout=2)


def start_panel(
    config: AddonConfig,
    pki: PkiManager,
    supervisor: OpenVpnSupervisor,
    stop_event: threading.Event,
) -> list[PanelServer]:
    def issue(name: str) -> dict[str, Any]:
        result = issue_client(config, pki, name)
        return {
            "status": "ok",
            "client_name": name,
            "action": result["action"],
            "export_path": str(result["export_path"]),
            "publish_path": str(result["publish_path"]) if result.get("publish_path") else None,
        }

    def revoke(name: str) -> dict[str, Any]:
        pki.revoke_client(name)
        sync_client_ips(config, pki)
        outcome = supervisor.restart("certificate revoked") if supervisor.is_running() else {"status": "ok"}
        return {"status": "ok", "client_name": name, "message": outcome.get("message", "revoked")}

    def export_profile(name: str) -> str:
        validate_client_name(name)
        if pki.client_status(name) == "missing":
            raise ValueError(f"unknown client: {name}")
        return render_client_config(config, name)

    def refresh_crl() -> dict[str, Any]:
        pki.refresh_crl()
        info = pki.crl_info()
        return {
            "status": "ok",
            "last_update": info.last_update,
            "next_update": info.next_update,
            "days_remaining": info.days_remaining,
        }

    def runtime_info() -> dict[str, Any]:
        checks = collect_health(config, pki)
        return {
            "openvpn_pid": supervisor.pid,
            "openvpn_running": supervisor.is_running(),
            "openvpn_uptime_seconds": supervisor.uptime_seconds(),
            "openvpn_restarts": supervisor.restarts,
            "openvpn_last_exit_code": supervisor.last_exit_code,
            "ip_forward": ip_forward_enabled(),
            "health": worst_status(checks),
            "log_tail": supervisor.log_tail(),
        }

    actions = PanelActions(
        issue_client=issue,
        revoke_client=revoke,
        export_profile=export_profile,
        refresh_crl=refresh_crl,
        restart_openvpn=lambda: supervisor.restart("manual request"),
        runtime_info=runtime_info,
    )

    servers: list[PanelServer] = []
    for address in panel_bind_addresses():
        try:
            server = PanelServer(config, actions, address, PANEL_PORT)
        except OSError as error:
            print(f"WARNING: unable to bind the add-on panel to {address}:{PANEL_PORT}: {error}", flush=True)
            continue
        server.start()
        servers.append(server)
        if address == "0.0.0.0":
            print(
                "WARNING: the Supervisor bridge address could not be detected, so the add-on panel listens "
                "on all interfaces. Do not forward this port from the internet.",
                flush=True,
            )

    if not servers:
        print(f"ERROR: the add-on panel could not start on port {PANEL_PORT}.", flush=True)
        return []
    bound = ", ".join(f"{server.address[0]}:{server.address[1]}" for server in servers)
    print(f"Add-on panel listening on {bound} (Home Assistant ingress).", flush=True)
    return servers


def load_and_validate() -> AddonConfig:
    config = load_config()
    validate_config(config)
    return config


def ensure_dirs(config: AddonConfig) -> None:
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
        config.share_rejected_dir,
    ]:
        path.mkdir(parents=True, exist_ok=True)


def sync_client_ips(config: AddonConfig, pki: PkiManager) -> dict[str, str]:
    names = [client.name for client in pki.list_clients() if client.status == "valid"]
    try:
        assignments = pki.ensure_client_ips(names)
    except (OSError, RuntimeError) as error:
        print(f"ERROR: unable to assign fixed client addresses: {error}", flush=True)
        return {}
    if assignments:
        print(f"Fixed client addresses: {json.dumps(assignments, sort_keys=True)}", flush=True)
    return assignments


def export_default_client(config: AddonConfig) -> None:
    if config.public_address == "0.0.0.0":
        print(
            "Skipping default client export because public_endpoint/server_host is not routable yet.",
            flush=True,
        )
        return
    try:
        result = export_client(config, config.client_name)
    except (OSError, ValueError) as error:
        print(f"WARNING: unable to export the default client profile: {error}", flush=True)
        return
    print(f"Exported default client profile: {result.export_path}", flush=True)


def issue_client(config: AddonConfig, pki: PkiManager, name: str) -> dict[str, Any]:
    outcome = pki.ensure_client(name)
    print(f"Issued client certificate: {name} ({outcome.action}) — {outcome.detail}", flush=True)
    sync_client_ips(config, pki)
    exported = export_client(config, name)
    return {
        "action": outcome.action,
        "detail": outcome.detail,
        "export_path": exported.export_path,
        "publish_path": exported.publish_path,
    }


def export_client(config: AddonConfig, name: str) -> ExportResult:
    validate_client_name(name)
    if config.public_address == "0.0.0.0":
        raise ValueError("Set public_endpoint or a concrete server_host before exporting a client profile")
    output = render_client_config(config, name)
    path = config.exports_dir / f"{name}.ovpn"
    path.write_text(output, encoding="utf-8")
    path.chmod(0o600)
    publish_path = publish_export(config, path)
    return ExportResult(path, publish_path)


def publish_export(config: AddonConfig, source_path: Path) -> Path | None:
    if not config.publish_profiles_to_share:
        return None
    target_path = config.share_exports_dir / source_path.name
    target_path.write_text(source_path.read_text(encoding="utf-8"), encoding="utf-8")
    # The profile embeds the client private key; keep it away from other add-ons where possible.
    target_path.chmod(0o600)
    return target_path


def remove_profile(config: AddonConfig, client_name: str) -> None:
    for path in [config.exports_dir / f"{client_name}.ovpn", config.share_exports_dir / f"{client_name}.ovpn"]:
        if path.exists():
            path.unlink()


def log_routing_intent(config: AddonConfig) -> None:
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


def process_pending_requests(config: AddonConfig) -> None:
    scan = scan_client_requests(config.share_requests_dir)
    if not scan.accepted and not scan.rejected:
        return

    print(
        f"Found {len(scan.accepted)} pending client request(s) and {len(scan.rejected)} invalid file(s) "
        f"in {config.share_requests_dir}",
        flush=True,
    )
    pki = PkiManager(config)

    for rejected in scan.rejected:
        print(f"Rejecting client request {rejected.request_path.name}: {rejected.reason}", flush=True)
        write_result(
            config.share_results_dir,
            rejected.request_path.name,
            {"status": "error", "message": rejected.reason, "request": rejected.request_path.name},
        )
        reject_request(config.share_rejected_dir, rejected.request_path)

    for request in scan.accepted:
        print(f"Processing client {request.action} request: {request.request_path.name}", flush=True)
        try:
            payload = process_client_request(config, pki, request.action, request.client_name)
        except Exception as error:  # noqa: BLE001 - a bad request must not stop the add-on
            payload = {
                "status": "error",
                "action": request.action,
                "client_name": request.client_name,
                "message": str(error),
            }
            print(f"ERROR: client {request.action} request failed: {error}", flush=True)
        result_path = write_result(config.share_results_dir, request.request_path.name, payload)
        archive_path = archive_request(config.share_archive_dir, request.request_path)
        print(f"Result file: {result_path}", flush=True)
        print(f"Archived request: {archive_path}", flush=True)


def process_client_request(config: AddonConfig, pki: PkiManager, action: str, client_name: str) -> dict[str, Any]:
    if action == "issue":
        result = issue_client(config, pki, client_name)
        return {
            "status": "ok",
            "action": "issue",
            "client_name": client_name,
            "certificate_action": result["action"],
            "detail": result["detail"],
            "internal_export_path": str(result["export_path"]),
            "published_profile_path": str(result["publish_path"]) if result.get("publish_path") else None,
        }

    if action == "revoke":
        pki.revoke_client(client_name)
        sync_client_ips(config, pki)
        remove_profile(config, client_name)
        print(f"Revoked client certificate: {client_name}", flush=True)
        return {
            "status": "ok",
            "action": "revoke",
            "client_name": client_name,
            "message": "Restart the add-on or use the panel to reload the revocation list.",
        }

    raise ValueError(f"Unsupported request action: {action}")


if __name__ == "__main__":
    sys.exit(main())
