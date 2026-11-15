# OpenVPN Server Add-on Docs

Full repository documentation:

- [README.md](https://github.com/Thweki/ha-addon-openvpn-server/blob/main/README.md)
- [README.zh-CN.md](https://github.com/Thweki/ha-addon-openvpn-server/blob/main/README.zh-CN.md)

## Experimental client endpoint failover

For homes with multiple WAN addresses or port forwards, exported profiles can try
alternate addresses for the same VPN server. This feature is **OFF by default**.
In the add-on options, for example:

```yaml
public_endpoint: "vpn.example.com"
public_port: 1194
experimental_client_failover: true
client_fallback_endpoints:
  - "backup.example.com"
  - "192.0.2.10:443"
client_connect_timeout: 30
```

Replace the example addresses with your own. Save and restart the add-on, then
download/export each client profile again and re-import it on the device. Already
installed profiles are not updated automatically.

The primary `public_endpoint` is tried first (or a concrete `server_host` when
empty), followed by the listed backups. Supply 1–8 DNS names or IPv4 addresses;
an omitted port uses `public_port`, even if the primary address specifies its own
port. Duplicate host/port pairs are omitted without changing order. IPv6 literals,
URLs, and per-endpoint protocol overrides are not supported.

All addresses must reach this same server using the configured `protocol` and
existing credentials. This does not create another listener or port forward.
The generated profile uses OpenVPN's
[multiple `remote` directives and `server-poll-timeout`](https://openvpn.net/community-docs/community-articles/openvpn-2-6-manual/)
to try another address after a connection attempt fails. The timeout is 1–300
seconds (default 30); DNS resolution and client app behavior can affect timing.
Established-session failure detection still uses the existing keepalive behavior.

Set `experimental_client_failover: false`, then re-export and re-import profiles
to return devices to a single address. Disabled settings are ignored by the add-on.

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
