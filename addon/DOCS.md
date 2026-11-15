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

### Keepalive and reconnect timers

Inside the add-on container, run `openvpn-addon keepalive` to print a read-only
JSON inventory from `/data/openvpn/runtime/server.conf`. This helps explain how
long a silent peer can remain connected before OpenVPN restarts its connection.
No settings are changed, and no keys, profiles, or referenced files are read.

The report lists `keepalive`, `ping`, `ping-restart`, and `ping-exit` directives
with source line numbers, values in seconds, and server or client-push scope.
For example, the default `keepalive 10 120` expands in server mode to server
`ping 10` / `ping-restart 240` and client pushes `ping 10` / `ping-restart 120`.
See the [OpenVPN manual](https://openvpn.net/community-docs/community-articles/openvpn-2-6-manual/)
for timer semantics. Zero explicit ping timers disable the corresponding timer;
keepalive is unrelated to the user-traffic idle timeout.

This is an on-disk inventory, not proof of settings received by a client or
loaded by the running process. Pending options changes are not rendered by this
command. Includes, per-client CCD overrides, scripts and client pull filters are
not evaluated. Repeated or conflicting directives remain visible with warnings;
helper expansions are illustrative, not resolved effective values. Malformed
timer entries are omitted with warnings that never echo their contents.

Exit code is `0` when the file was read (including reports with warnings), or `1`
when it is missing or unreadable. A missing file does not fall back to defaults.

## Files and paths

- Client profiles: `/data/openvpn/exports/` (mode `0600`)
- Optional `/share` copies: `/share/openvpn_server/clients/` (only with `publish_profiles_to_share`)
- Requests and results: `/share/openvpn_server/requests/`, `results/`, `archive/`, `rejected/`
- PKI: `/data/openvpn/pki/`
- Runtime: `/data/openvpn/runtime/` (`server.conf`, OpenVPN status, per-client `ccd/`)
