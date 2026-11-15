# OpenVPN Server Add-on Docs

Full repository documentation:

- [README.md](https://github.com/Thweki/ha-addon-openvpn-server/blob/main/README.md)
- [README.zh-CN.md](https://github.com/Thweki/ha-addon-openvpn-server/blob/main/README.zh-CN.md)

## Troubleshooting

Start with **Open Web UI -> Health**. Every check below is reported there together with the reason.

### Clients connect but cannot reach LAN devices

- `nat_enabled` must be on. With it off, clients can reach the VPN server only.
- `ip_forward` must be enabled on the host; the health check shows its current value.
- `lan_networks` must list the subnet you want to reach, for example `192.168.1.0/24`.
- The health check verifies that the add-on's `OPENVPN_ADDON` NAT chain and `FORWARD` jumps exist.

### Clients cannot connect at all

- The CRL must not be expired; the health check shows the remaining days. Restart the add-on to rebuild it.
- Check that the router forwards the external port to `listen_port` on this host. If `public_port` differs from `listen_port`, that mapping is required.
- Port scanners do hit the VPN port; a `tls-error` from an unknown address in the log is normal noise.

### A client profile was issued but the device is rejected

- The certificate may be revoked. The panel shows `revoked` and the client must be issued under a new name if the old key may have leaked.
- Revoking rebuilds the CRL and briefly restarts OpenVPN so the change takes effect immediately.

### The add-on log shows "OpenVPN exited with code ..."

The add-on keeps running and retrying with backoff so the panel stays reachable, and prints the reason it stopped. Check the health panel and the diagnostics bundle.

## Diagnostics

The panel's **Download diagnostics** produces a text bundle with the status payload, runtime details and the rendered `server.conf`. Attach it to bug reports; it does not contain private keys.

## Usage metrics

Run these commands inside the add-on container. Metrics are available without
enabling an option or restarting OpenVPN:

```sh
openvpn-addon metrics
openvpn-addon metrics --format json > /tmp/openvpn-usage.json
openvpn-addon metrics --format prometheus > /tmp/openvpn-usage.prom
```

The command reads the existing status file only (plus add-on settings). It does
not inspect PKI or profiles, run network checks, change state, or expose a new
HTTP port. Output contains no names, addresses, settings, keys, or per-client
labels. Redirection writes the output file; the command itself writes only stdout.

JSON keys correspond to Prometheus metric names with the `openvpn_addon_` prefix:

| JSON key | Meaning |
| --- | --- |
| `status_readable` | 1 if the status file was read successfully; otherwise 0. |
| `status_valid` | 1 if the snapshot has a client header, valid session rows, an END marker, and did not change during the read. |
| `status_fresh` | 1 if valid and the file modification time is no older than `max(60, 2 * status_interval)` seconds, and not in the future. |
| `status_age_seconds` | Seconds since file modification (clamped to zero for future timestamps). |
| `connected_sessions` | Number of connected sessions, including multiple sessions sharing a certificate. |
| `connected_clients` | Number of distinct connected certificate common names. |
| `session_received_bytes` | Sum of bytes the server received for currently connected sessions. |
| `session_sent_bytes` | Sum of bytes the server sent for currently connected sessions. |

The collector supports [OpenVPN status versions 1, 2 and 3](https://openvpn.net/community-docs/community-articles/openvpn-2-6-manual/).
A complete, fresh snapshot with no clients reports zero usage. Missing,
unreadable, incomplete, malformed, changing, or stale snapshots report unknown
usage: JSON `null`, or omitted Prometheus samples. Availability flags remain
present. The modification time is a freshness heuristic, not proof that the
server is still running; after shutdown, a snapshot can remain fresh for the
window above. Retry a partial snapshot on the next collection.

All samples use the [Prometheus text format](https://prometheus.io/docs/instrumenting/exposition_formats/)
and are gauges. Byte values describe only the sessions in the snapshot: they
reset on reconnect and disappear on disconnect. They are not lifetime traffic
counters or suitable inputs to `rate()`. No traffic history is stored.

Exit status is 0 when a report is emitted, even if usage is unavailable; consumers
should check `status_fresh`. Invalid settings return 1 with a generic error on
stderr. An existing Prometheus textfile collector can consume the `.prom` output;
publish scheduled exports by writing a temporary file in its directory and
renaming it after the command succeeds. Installing or scheduling a collector is
outside this command.

## Files and paths

- Client profiles: `/data/openvpn/exports/` (mode `0600`)
- Optional `/share` copies: `/share/openvpn_server/clients/` (only with `publish_profiles_to_share`)
- Requests and results: `/share/openvpn_server/requests/`, `results/`, `archive/`, `rejected/`
- PKI: `/data/openvpn/pki/`
- Runtime: `/data/openvpn/runtime/` (`server.conf`, OpenVPN status, per-client `ccd/`)
