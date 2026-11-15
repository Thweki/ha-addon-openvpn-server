# OpenVPN Server Add-on Docs

Full repository documentation:

- [README.md](https://github.com/Thweki/ha-addon-openvpn-server/blob/main/README.md)
- [README.zh-CN.md](https://github.com/Thweki/ha-addon-openvpn-server/blob/main/README.zh-CN.md)

## Experimental server certificate pinning

To bind newly exported client profiles to the current server certificate, set:

```yaml
experimental_server_pinning: true
```

This option defaults **off**. Save the add-on options and restart, then download
fresh profiles from the panel or run `openvpn-addon client export <name>` inside
the container and import the new profiles on your devices. Use clients supporting
OpenVPN 2.6's [`peer-fingerprint` directive](https://openvpn.net/community-docs/community-articles/openvpn-2-6-manual/).
Older clients may reject this directive.

The exporter derives a SHA-256 fingerprint from `pki/issued/server.crt` and embeds
it in each profile. The client requires that exact server certificate, preventing
another server certificate from being accepted merely because it uses the same
CA. The profile retains its CA and `remote-cert-tls server` settings. No server
private key is read. If the server certificate cannot be parsed, export fails
instead of producing an unpinned profile; existing exports are left intact.

Replacing or renewing the server certificate changes its fingerprint: re-export
and redistribute profiles before reconnecting those devices. Changing only the
public endpoint does not change the pin. Existing downloaded profiles are never
updated automatically. To disable pinning, set the option to `false`, restart,
and re-export and re-import profiles; previously pinned copies remain pinned.

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
