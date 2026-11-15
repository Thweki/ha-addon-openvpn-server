# OpenVPN Server Add-on Docs

Full repository documentation:

- [README.md](https://github.com/Thweki/ha-addon-openvpn-server/blob/main/README.md)
- [README.zh-CN.md](https://github.com/Thweki/ha-addon-openvpn-server/blob/main/README.zh-CN.md)

## Troubleshooting

Start with **Open Web UI -> Health**. Every check below is reported there together with the reason.

### Clients connect but cannot reach LAN devices

- Check whether the client's Wi-Fi/Ethernet subnet overlaps the VPN or home LAN using the command below.
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

### Check a remote client's local networks

Inside the add-on container, supply the IPv4 subnet(s) used by the client before it connects:

```sh
openvpn-addon check-client-network 192.168.1.0/24
openvpn-addon check-client-network 192.168.1.42/24 10.20.0.0/16
```

The read-only JSON report compares those networks with the configured VPN subnet and
the routes pushed from `lan_networks` and `extra_routes`. It identifies overlapping
address ranges, explains the routing risk and suggests a remedy. Host bits are
normalized and duplicate networks are checked once. A prefix is required; IPv6 is
not supported. Exit codes are `0` for no overlaps, `1` for overlaps and `2` for
invalid input or configuration.

For example, a client on `192.168.1.0/24` and a home LAN on the same subnet may
reach local devices instead of home devices. A more specific pushed route can
instead hide local devices. Choose non-overlapping subnets where possible;
`redirect_gateway` does not resolve overlapping LAN addresses.

This checks configured options, not live client routes or connection health. It
does not contact the client, read certificates or keys, change routes, or restart
OpenVPN. NAT being disabled does not suppress the check because routes are still
pushed. No feature flag is needed for this read-only command.

### Download diagnostics

The panel's **Download diagnostics** produces a text bundle with the status payload, runtime details and the rendered `server.conf`. Attach it to bug reports; it does not contain private keys.

## Files and paths

- Client profiles: `/data/openvpn/exports/` (mode `0600`)
- Optional `/share` copies: `/share/openvpn_server/clients/` (only with `publish_profiles_to_share`)
- Requests and results: `/share/openvpn_server/requests/`, `results/`, `archive/`, `rejected/`
- PKI: `/data/openvpn/pki/`
- Runtime: `/data/openvpn/runtime/` (`server.conf`, OpenVPN status, per-client `ccd/`)
