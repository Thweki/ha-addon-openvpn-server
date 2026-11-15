# OpenVPN Server Add-on Docs

Full repository documentation:

- [README.md](https://github.com/Thweki/ha-addon-openvpn-server/blob/main/README.md)
- [README.zh-CN.md](https://github.com/Thweki/ha-addon-openvpn-server/blob/main/README.zh-CN.md)

## Experimental connection rate limit

To reduce the server load from bursts of new VPN connections, opt in to a
server-wide admission rate limit in the add-on options:

```yaml
experimental_connection_rate_limit: true
connection_rate_limit: 10
connection_rate_window: 60
```

Restart the add-on to apply. This renders `connect-freq 10 60` for either UDP or
TCP. The count accepts 1–1000 connections and the window accepts 1–3600 seconds.
All clients share the budget; it does not cap simultaneous clients or disconnect
established sessions. A burst can refuse legitimate reconnects too, so allow
enough headroom for all your devices reconnecting after an outage. The panel's
Health warnings show the configured rate while enabled.

The flag defaults to `false`. Setting the count/window alone does not enable it.
To remove the limit, set the flag back to `false` and restart. Client profiles do
not need to be downloaded again.

OpenVPN's [`connect-freq` documentation](https://github.com/OpenVPN/openvpn/blob/release/2.6/doc/man-sections/server-options.rst)
describes which connection attempts count toward this limit. On OpenVPN 2.6 UDP,
it applies after the initial three-way handshake; the separate initial-packet
response limiter remains at OpenVPN's default. Keep `tls_crypt` enabled; this
feature is not a complete DoS defense.

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

## Files and paths

- Client profiles: `/data/openvpn/exports/` (mode `0600`)
- Optional `/share` copies: `/share/openvpn_server/clients/` (only with `publish_profiles_to_share`)
- Requests and results: `/share/openvpn_server/requests/`, `results/`, `archive/`, `rejected/`
- PKI: `/data/openvpn/pki/`
- Runtime: `/data/openvpn/runtime/` (`server.conf`, OpenVPN status, per-client `ccd/`)
