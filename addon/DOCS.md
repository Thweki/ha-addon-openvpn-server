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

## Importing a settings snapshot

From a shell inside the add-on container, validate a JSON settings snapshot made
by `backup-config` (introduced separately in PR #20):

```sh
openvpn-addon import-config /data/openvpn-settings.json --check
openvpn-addon import-config /data/openvpn-settings.json --output /data/imported-options.json
```

`--check` writes nothing. `--output` stages a complete, owner-only (`0600`)
options file atomically; its directory must exist and its filename must be new.
Existing files and destination symlinks are refused. Neither mode reads current
settings, changes live settings, restarts OpenVPN, or accesses PKI.

To finish restoring in Home Assistant, copy the staged settings into the add-on
Configuration editor, save, and restart. The command does not update Supervisor's
stored options. It never prints snapshot contents or input paths. Keep both files
private because they include endpoint and network details.

Only complete snapshots using this version's option names and JSON types are
accepted (including string `dns`, `lan_networks`, and `extra_routes`). Missing,
unknown, duplicate, or PKI keys, nested objects, inline key/certificate material,
unsafe directive text, invalid settings, and files larger than 64 KiB are rejected
with a nonzero exit status before an output is created. Legacy aliases and partial
configuration files are not snapshots. This restores settings only; recovering
certificates, private keys, revocations, or fixed client assignments requires a
full Home Assistant backup.

## Files and paths

- Client profiles: `/data/openvpn/exports/` (mode `0600`)
- Optional `/share` copies: `/share/openvpn_server/clients/` (only with `publish_profiles_to_share`)
- Requests and results: `/share/openvpn_server/requests/`, `results/`, `archive/`, `rejected/`
- PKI: `/data/openvpn/pki/`
- Runtime: `/data/openvpn/runtime/` (`server.conf`, OpenVPN status, per-client `ccd/`)
