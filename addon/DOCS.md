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

### Experimental CRL renewal preview

Inside the add-on container, explicitly opt in for each invocation:

```sh
openvpn-addon crl-renew-preview --experimental
```

Without `--experimental`, the command is disabled and exits before reading options or PKI.
The JSON plan simulates one check using the auto-renewal policy proposed in
[PR #27](https://github.com/Thweki/ha-addon-openvpn-server/pull/27): renew within seven
days of CRL expiry, or half of `crl_valid_days` for shorter lifetimes. An expired or
missing CRL would be renewed; a missing PKI index or unreadable CRL blocks the plan.
It reports `renew`, `keep`, or `blocked`, the reason, the renewal window in seconds,
the eligibility time, and the proposed expiry if renewed now. Times are UTC. Exit
status is `0` for a completed plan (including `renew`) and `2` when disabled or blocked.

This is a point-in-time policy simulation, not a signing readiness check: it does
not inspect keys, validate the index contents or CRL signature, or guarantee that
EasyRSA could sign successfully. Concurrent PKI changes can make a plan stale.
It never runs EasyRSA, creates locks or directories, writes PKI, renews certificates,
or restarts OpenVPN. It does not enable automatic renewal or change existing startup
behavior; startup still rebuilds the CRL. No restart is needed to run the preview.

## Files and paths

- Client profiles: `/data/openvpn/exports/` (mode `0600`)
- Optional `/share` copies: `/share/openvpn_server/clients/` (only with `publish_profiles_to_share`)
- Requests and results: `/share/openvpn_server/requests/`, `results/`, `archive/`, `rejected/`
- PKI: `/data/openvpn/pki/`
- Runtime: `/data/openvpn/runtime/` (`server.conf`, OpenVPN status, per-client `ccd/`)
