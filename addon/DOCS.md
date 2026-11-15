# OpenVPN Server Add-on Docs

Full repository documentation:

- [README.md](https://github.com/Thweki/ha-addon-openvpn-server/blob/main/README.md)
- [README.zh-CN.md](https://github.com/Thweki/ha-addon-openvpn-server/blob/main/README.zh-CN.md)

## Minimum TLS version

The `tls_version_min` option controls the minimum TLS version accepted for the
OpenVPN control channel, in both the server configuration and exported client
profiles. It defaults to `"1.2"`, preserving the existing policy (TLS 1.2 or newer).
To require TLS 1.3, set this in the add-on configuration:

```yaml
tls_version_min: "1.3"
```

Keep the value quoted when editing YAML. Only `"1.2"` and `"1.3"` are accepted;
older versions and automatic fallback are not supported. This uses OpenVPN's
standard [`tls-version-min` directive](https://openvpn.net/community-docs/community-articles/openvpn-2-6-manual/).

Save and restart the add-on, then download updated profiles from the panel and
import them on your devices. Previously downloaded profiles and non-default
profiles already in `/share` are not automatically updated; re-export those if
you use them. No certificates need to be reissued.

Choose `"1.3"` only when every client's OpenVPN and TLS library support TLS 1.3.
The server will reject TLS 1.2-only clients even if they still use an old profile.
If needed, restore `"1.2"`, restart, and download/import fresh profiles again.
This setting does not change data-channel ciphers, certificate verification, or
`tls_crypt`.

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
