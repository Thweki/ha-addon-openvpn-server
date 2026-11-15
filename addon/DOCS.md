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

### MTU, MSS and fragmentation report

Inside the add-on container, run:

```sh
openvpn-addon mtu
```

This read-only JSON report helps investigate tunnels that connect but stall during
transfers. It lists explicit `tun-mtu`, `tun-mtu-extra`, `link-mtu`, `mssfix`,
`fragment` and `max-packet-size` occurrences from the existing runtime `server.conf`,
separating server settings from global `push` statements and preserving line numbers
and repeated values. For example, `mssfix 1300 mtu` becomes
`{"line": 12, "arguments": [1300, "mtu"]}` under `settings.server.mssfix`.

Empty lists mean no explicit setting, not zero or disabled. The shipped template
currently has no explicit overrides. Bare `mssfix` has an empty argument list;
`mssfix 0` has `[0]`. Defaults, implicit settings, include files, per-client
overrides and client acceptance are not resolved. This is disk configuration,
not a measurement of path MTU or confirmation of running settings. Syntax is
recognized using the [OpenVPN 2.6 link options](https://github.com/OpenVPN/openvpn/blob/release/2.6/doc/man-sections/link-options.rst);
the command does not validate compatibility or recommend a packet size.

Only recognized numeric arguments and mode keywords are included. Inline blocks
are skipped, and keys, profiles and referenced files are never opened. The command
does not load add-on options, initialize state, send probes or change settings.
Missing, unreadable, non-UTF-8 and symlinked runtime files are reported safely.
Exit status is `0` for a complete inventory within this scope, `1` for warnings
(such as repeated or malformed directives or unevaluated includes), and `2` when
the server configuration cannot be read. Read `warnings` and `notes` before
interpreting an empty or incomplete report.

## Files and paths

- Client profiles: `/data/openvpn/exports/` (mode `0600`)
- Optional `/share` copies: `/share/openvpn_server/clients/` (only with `publish_profiles_to_share`)
- Requests and results: `/share/openvpn_server/requests/`, `results/`, `archive/`, `rejected/`
- PKI: `/data/openvpn/pki/`
- Runtime: `/data/openvpn/runtime/` (`server.conf`, OpenVPN status, per-client `ccd/`)
