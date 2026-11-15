# OpenVPN Server Add-on Docs

Full repository documentation:

- [README.md](https://github.com/Thweki/ha-addon-openvpn-server/blob/main/README.md)
- [README.zh-CN.md](https://github.com/Thweki/ha-addon-openvpn-server/blob/main/README.zh-CN.md)

## Experimental tls-auth compatibility

`tls-crypt` is already enabled by default and remains the recommended mode.
For clients that require `tls-auth`, opt in through the add-on configuration:

```yaml
tls_crypt: false
experimental_tls_auth: true
```

The experimental flag defaults to `false`. Enabling both options is rejected
before startup changes any files. Restart the add-on after changing modes, then
download a fresh profile for **every client** from the panel (or use
`openvpn-addon client export NAME`) and re-import it on each device. Existing
profiles will not connect until updated. Previously exported files are not all
automatically refreshed; replace any copies you distributed, including `/share`
copies if publishing is enabled.

The server uses `tls-auth` direction `0`; exported profiles embed the same static
key with `key-direction 1`. The existing `/data/openvpn/pki/ta.key` is reused
across restarts and mode changes; switching modes does not rotate it or replace
certificates. The configured `auth` digest is used on both sides. This adds
control-packet authentication without the extra control-channel encryption of
`tls-crypt`; TLS and data-channel encryption remain enabled. See the
[OpenVPN manual](https://openvpn.net/community-docs/community-articles/openvpn-2-6-manual/)
for protocol details. This option does not change cipher or TLS-version support.

To return to the default mode, set `experimental_tls_auth: false` and
`tls_crypt: true`, restart, and refresh every client's profile again. Setting both
to `false` retains the existing behavior with no static-key control-channel wrap.

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
