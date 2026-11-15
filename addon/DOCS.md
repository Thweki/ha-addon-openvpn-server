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

## Configuration backup

From a shell inside the add-on container, save a snapshot before changing settings:

```sh
openvpn-addon backup-config --output /data/openvpn-settings.json
```

The destination directory must already exist and the file must be new. The command
atomically creates an owner-only (`0600`) JSON file and refuses to overwrite files
or follow destination symlinks. It prints only a success or error message, never
option values. Failure returns a nonzero exit status and removes temporary files.

The snapshot includes effective defaults and uses Home Assistant option names
(`dns`, `lan_networks`, `extra_routes`), so it can also be loaded as an
`options.json` file. Unknown options are omitted. To reapply settings in Home
Assistant, use the snapshot values in the add-on Configuration editor, save, and
restart the add-on. There is no automatic restore command. Keep the snapshot in
private storage: it contains endpoint and network details even though it contains
no private keys. There is no automatic copy to `/share`.

This is a configuration-only backup. It excludes the CA, certificates, private
keys, revocations, fixed client address assignments, profiles and logs. It cannot
recover VPN identities after data loss; retain a separate full Home Assistant
backup for that purpose.

## Files and paths

- Client profiles: `/data/openvpn/exports/` (mode `0600`)
- Optional `/share` copies: `/share/openvpn_server/clients/` (only with `publish_profiles_to_share`)
- Requests and results: `/share/openvpn_server/requests/`, `results/`, `archive/`, `rejected/`
- PKI: `/data/openvpn/pki/`
- Runtime: `/data/openvpn/runtime/` (`server.conf`, OpenVPN status, per-client `ccd/`)
