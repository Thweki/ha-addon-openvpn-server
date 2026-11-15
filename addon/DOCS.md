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

### Inspect pushed routes and client iroutes

Inside the add-on container, run:

```sh
openvpn-addon routes
```

This read-only command prints JSON from the rendered `runtime/server.conf` and,
when that file enables it, the add-on's `runtime/ccd/` directory. Each entry has
its source file, line number, directive and normalized network, with optional
gateway/metric. It includes IPv4/IPv6 pushed routes, client `iroute` directives,
pushed `redirect-gateway` flags and `push-reset`. CCD filenames identify clients;
`ccd/DEFAULT` is OpenVPN's fallback file, not a client identity.

Use it to check which explicit routes were rendered, or which client owns a
subnet, without reading private keys or exported profiles. It works even when
saved options are invalid, does not regenerate configuration, and ignores stale
CCD files when the rendered server configuration disables that directory.

The report describes **files on disk**, not confirmed live pushes or kernel
routes. It does not calculate inheritance, helper-generated routes, or the final
per-client push list. OpenVPN may suppress a pushed route matching a client's
iroute; clients may filter pushes. Includes, scripts, plugins and `push-remove`
are not evaluated and produce warnings. See the
[OpenVPN manual](https://openvpn.net/community-docs/community-articles/openvpn-2-6-manual/)
for these routing rules. Hand-edited routes using hostnames or other unsupported
operands are omitted with a warning; input values are not echoed in errors.

Exit status is `0` when all supported explicit directives were read (`complete:
true`), or `1` for a partial report with warnings, including missing/unreadable
files. No runtime file yet is different from an empty route list. Symlinked files,
special files and external CCD directories are not inspected. Output contains
client names and network topology; review it before sharing.

## Files and paths

- Client profiles: `/data/openvpn/exports/` (mode `0600`)
- Optional `/share` copies: `/share/openvpn_server/clients/` (only with `publish_profiles_to_share`)
- Requests and results: `/share/openvpn_server/requests/`, `results/`, `archive/`, `rejected/`
- PKI: `/data/openvpn/pki/`
- Runtime: `/data/openvpn/runtime/` (`server.conf`, OpenVPN status, per-client `ccd/`)
