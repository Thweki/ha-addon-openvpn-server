from __future__ import annotations

import json
import threading
import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .config import AddonConfig
from .network import _host_ipv4_addresses  # noqa: PLC2701 - shared interface discovery
from .status import collect_status


MAX_BODY_BYTES = 64 * 1024


@dataclass(slots=True)
class PanelActions:
    """Callbacks the running add-on provides to the panel."""

    issue_client: Callable[[str], dict[str, Any]]
    revoke_client: Callable[[str], dict[str, Any]]
    export_profile: Callable[[str], str]
    refresh_crl: Callable[[], dict[str, Any]]
    restart_openvpn: Callable[[], dict[str, Any]]
    runtime_info: Callable[[], dict[str, Any]]


class PanelServer:
    def __init__(self, config: AddonConfig, actions: PanelActions, host: str, port: int) -> None:
        self.config = config
        self.actions = actions
        handler = _build_handler(config, actions)
        self._httpd = ThreadingHTTPServer((host, port), handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="panel", daemon=True)

    @property
    def address(self) -> tuple[str, int]:
        return self._httpd.server_address[:2]

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def panel_bind_addresses() -> list[str]:
    """Bind the Supervisor bridge and loopback so the panel is not reachable from the LAN.

    Home Assistant ingress proxies to the add-on address, which for a host-network add-on is the
    bridge; loopback covers setups where the proxy connects locally instead.
    """
    addresses: list[str] = []
    bridge: str | None = None
    for name, address in _host_ipv4_addresses():
        if name in {"hassio", "docker0"} or str(address.ip).startswith("172.30.32."):
            bridge = str(address.ip)
            break
    if bridge is None:
        # Bridge not found: fall back to all interfaces and warn, otherwise the panel is unreachable.
        return ["0.0.0.0"]
    return [bridge, "127.0.0.1"]


def _build_handler(config: AddonConfig, actions: PanelActions) -> type[BaseHTTPRequestHandler]:
    class PanelHandler(BaseHTTPRequestHandler):
        server_version = "openvpn-addon-panel"

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
            if path == "/":
                self._send_html(PAGE)
                return
            if path == "/api/status":
                self._send_json(collect_status(config))
                return
            if path == "/api/diagnostics":
                self._send_text(_diagnostics(config, actions))
                return
            profile = _match_client_route(path, "/api/clients/", "/profile")
            if profile:
                try:
                    body = actions.export_profile(profile)
                except Exception as error:  # noqa: BLE001 - surfaced to the caller
                    self._send_json({"status": "error", "message": str(error)}, HTTPStatus.BAD_REQUEST)
                    return
                self._send_text(body, filename=f"{profile}.ovpn")
                return
            self._send_json({"status": "error", "message": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
            try:
                payload = self._read_json()
            except ValueError as error:
                self._send_json({"status": "error", "message": str(error)}, HTTPStatus.BAD_REQUEST)
                return

            if path == "/api/clients":
                name = str(payload.get("client_name", "")).strip()
                self._call(actions.issue_client, name)
                return
            if path == "/api/crl/refresh":
                self._call(lambda _: actions.refresh_crl(), "")
                return
            if path == "/api/openvpn/restart":
                self._call(lambda _: actions.restart_openvpn(), "")
                return
            revoke = _match_client_route(path, "/api/clients/", "/revoke")
            if revoke:
                self._call(actions.revoke_client, revoke)
                return
            self._send_json({"status": "error", "message": "not found"}, HTTPStatus.NOT_FOUND)

        def _call(self, action: Callable[[str], dict[str, Any]], argument: str) -> None:
            try:
                self._send_json(action(argument))
            except Exception as error:  # noqa: BLE001 - surfaced to the caller
                self._send_json({"status": "error", "message": str(error)}, HTTPStatus.BAD_REQUEST)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                raise ValueError("request body too large")
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise ValueError(f"invalid JSON body: {error}") from error
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload

        def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, text: str) -> None:
            body = text.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, text: str, filename: str | None = None) -> None:
            body = text.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: Any) -> None:  # pragma: no cover - log plumbing
            print(f"[panel] {self.address_string()} {fmt % args}", flush=True)

    return PanelHandler


def _match_client_route(path: str, prefix: str, suffix: str) -> str | None:
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    name = path[len(prefix) : -len(suffix)].strip("/")
    if not name or "/" in name:
        return None
    return name


def _diagnostics(config: AddonConfig, actions: PanelActions) -> str:
    import datetime

    lines = [
        "# OpenVPN add-on diagnostics",
        f"generated: {datetime.datetime.now(datetime.timezone.utc).isoformat()}",
        "",
        "## status",
        json.dumps(collect_status(config), ensure_ascii=False, indent=2),
        "",
        "## runtime",
        json.dumps(actions.runtime_info(), ensure_ascii=False, indent=2, default=str),
        "",
        "## server.conf (paths only)",
        _redacted_server_conf(config),
        "",
    ]
    return "\n".join(lines)


def _redacted_server_conf(config: AddonConfig) -> str:
    path = config.server_conf_path
    if not path.exists():
        return "(not rendered yet)"
    return path.read_text(encoding="utf-8", errors="replace")


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenVPN Server</title>
<style>
:root{color-scheme:light dark;--bg:#fafafa;--fg:#1c1c1c;--card:#fff;--border:#e0e0e0;--muted:#707070;--ok:#2e7d32;--warn:#ed6c02;--err:#c62828;--accent:#03a9f4}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--fg:#e8e8e8;--card:#1b1f24;--border:#2c3138;--muted:#9aa0a6}}
*{box-sizing:border-box}body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:0 0 8px}
.grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));max-width:1100px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--border)}
th{color:var(--muted);font-weight:600}
button{cursor:pointer;border:1px solid var(--border);background:transparent;color:var(--fg);border-radius:8px;padding:5px 10px;font-size:13px}
button.primary{background:var(--accent);border-color:var(--accent);color:#00131c;font-weight:600}
input[type=text]{background:transparent;color:var(--fg);border:1px solid var(--border);border-radius:8px;padding:6px 8px;font-size:13px;width:100%}
.row{display:flex;gap:8px;align-items:center;margin-top:8px}
.badge{display:inline-block;border-radius:999px;padding:1px 8px;font-size:12px;border:1px solid currentColor}
.ok{color:var(--ok)}.warn{color:var(--warn)}.err{color:var(--err)}.muted{color:var(--muted)}
ul.health{list-style:none;margin:0;padding:0}ul.health li{padding:4px 0;border-bottom:1px solid var(--border);font-size:13px}
code{background:rgba(127,127,127,.15);border-radius:4px;padding:1px 4px}
#toast{position:fixed;left:16px;bottom:16px;background:var(--card);border:1px solid var(--border);border-radius:10px;padding:10px 14px;display:none;max-width:60ch}
</style>
</head>
<body>
<div class="grid">
  <div class="card" style="grid-column:1/-1">
    <h1>OpenVPN Server</h1>
    <div id="summary" class="muted">loading…</div>
  </div>

  <div class="card">
    <h2>Health</h2>
    <ul class="health" id="health"></ul>
  </div>

  <div class="card">
    <h2>Clients</h2>
    <table><thead><tr><th>name</th><th>state</th><th>VPN IP</th><th>online</th><th></th></tr></thead>
    <tbody id="clients"></tbody></table>
    <div class="row">
      <input type="text" id="newclient" placeholder="new client name">
      <button class="primary" id="issue">Issue</button>
    </div>
  </div>

  <div class="card" style="grid-column:1/-1">
    <h2>Maintenance</h2>
    <div class="row">
      <button id="refreshcrl">Rebuild CRL</button>
      <button id="restart">Restart OpenVPN</button>
      <button id="diag">Download diagnostics</button>
      <button id="reload">Refresh page data</button>
    </div>
    <p class="muted" style="margin:10px 0 0">Revoking a client rebuilds the CRL and briefly restarts OpenVPN so the change takes effect immediately.</p>
  </div>
</div>
<div id="toast"></div>
<script>
const $ = (id) => document.getElementById(id);
function toast(message, isError){
  const el = $("toast");
  el.textContent = message;
  el.style.display = "block";
  el.className = isError ? "err" : "";
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.style.display = "none"; }, 6000);
}
async function api(path, options){
  const response = await fetch(path, options);
  const text = await response.text();
  let payload;
  try { payload = JSON.parse(text); } catch(e) { payload = {raw: text}; }
  if (!response.ok) throw new Error(payload.message || payload.raw || response.statusText);
  return payload;
}
function badge(status){
  const cls = status === "ok" ? "ok" : status === "warn" ? "warn" : "err";
  return `<span class="badge ${cls}">${status}</span>`;
}
async function load(){
  const status = await api("api/status");
  const s = status.summary;
  const worst = status.health.status;
  $("summary").innerHTML =
    `${badge(worst)} &nbsp; listens on <code>${s.listen_address}</code> · clients connect to <code>${s.public_address}</code>`
    + ` · protocol <code>${s.protocol}</code>`
    + ` · LAN access <b>${s.nat_enabled && s.lan_access ? "on" : "off"}</b>`
    + ` · routes <code>${status.routes.join(", ") || "none"}</code>`
    + ` · CRL ${status.crl.days_remaining ?? "?"} day(s) left`;
  $("health").innerHTML = status.health.checks
    .map((c) => `<li>${badge(c.status)} <b>${c.name}</b><br><span class="muted">${c.detail}</span></li>`).join("");
  $("clients").innerHTML = status.clients.map((c) => `
    <tr>
      <td><code>${c.name}</code></td>
      <td>${c.status}</td>
      <td>${c.vpn_address || "—"}</td>
      <td>${c.online ? "yes" : "no"}</td>
      <td>
        <button data-download="${c.name}">.ovpn</button>
        ${c.status === "valid" ? `<button data-revoke="${c.name}">revoke</button>` : ""}
      </td>
    </tr>`).join("") || `<tr><td colspan="5" class="muted">no clients yet</td></tr>`;
}
document.addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  try {
    if (button.dataset.download){
      window.location = `api/clients/${encodeURIComponent(button.dataset.download)}/profile`;
    } else if (button.dataset.revoke){
      if (!confirm(`Revoke ${button.dataset.revoke}?`)) return;
      const result = await api(`api/clients/${encodeURIComponent(button.dataset.revoke)}/revoke`, {method:"POST"});
      toast(`revoked ${result.client_name}`); load();
    } else if (button.id === "issue"){
      const name = $("newclient").value.trim();
      if (!name) return;
      const result = await api("api/clients", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({client_name: name})});
      toast(`issued ${result.client_name} (${result.action})`); $("newclient").value = ""; load();
    } else if (button.id === "refreshcrl"){
      const result = await api("api/crl/refresh", {method:"POST"});
      toast(`CRL rebuilt, next update ${result.next_update}`); load();
    } else if (button.id === "restart"){
      const result = await api("api/openvpn/restart", {method:"POST"});
      toast(result.message); load();
    } else if (button.id === "diag"){
      window.location = "api/diagnostics";
    } else if (button.id === "reload"){
      load();
    }
  } catch (error) {
    toast(String(error.message || error), true);
  }
});
load().catch((error) => toast(String(error.message || error), true));
</script>
</body>
</html>
"""
