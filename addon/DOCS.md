# OpenVPN Server Add-on Docs

Full repository documentation:

- [README.md](https://github.com/Thweki/ha-addon-openvpn-server/blob/main/README.md)
- [README.zh-CN.md](https://github.com/Thweki/ha-addon-openvpn-server/blob/main/README.zh-CN.md)

## Experimental IPv6 tunnel addresses

IPv6 tunnel support is **off by default**. It adds a second address family inside
the existing VPN, so clients can reach IPv6 services on the VPN server. Enable it
in the add-on options, then restart the add-on and reconnect clients:

```yaml
experimental_ipv6: true
vpn_ipv6_subnet: "fd42:6f70:656e:8::/64"
device: tun
```

Choose a dedicated unique-local (ULA) or assigned global-unicast **/64 network**
that does not overlap your LAN, client networks or other VPNs. Host bits must be
zero. The default is an example ULA; choose your own prefix if it conflicts.
The host and client operating systems must have IPv6 enabled on their TUN devices.

The server uses the prefix's `::1` address (in this example,
`fd42:6f70:656e:8::1`). OpenVPN assigns IPv6 addresses to clients and pushes their
tunnel configuration using
[`server-ipv6`](https://openvpn.net/community-docs/community-articles/openvpn-2-6-manual/).
Existing exported profiles already accept server pushes; they do not need to be
reissued. After reconnecting, test with `ping -6 fd42:6f70:656e:8::1` from a client
whose firewall allows ICMPv6. Services must listen on IPv6 to be reachable there.
For client-to-client access, the existing `client_to_client` option must be enabled.

This feature configures **tunnel addressing only**. The public endpoint and
transport options are unchanged. It does not configure IPv6 LAN routes, DNS,
forwarding, firewall rules or NAT. `nat_enabled`, `lan_networks`, `extra_routes`,
and `redirect_gateway` continue to apply to IPv4 only. With `fixed_client_ips`
and a separate certificate per device, each client also gets a stable IPv6
address derived from its saved IPv4 address (for example `10.8.0.2` maps to
`fd42:6f70:656e:8:0:1:a08:2`). These addresses sit above OpenVPN's dynamic pool;
no separate IPv6 lease database is needed. Otherwise OpenVPN assigns dynamic
IPv6 addresses. Changing the IPv6 prefix changes all IPv6 assignments.
IPv6 internet traffic is neither redirected nor
blocked, even when `redirect_gateway` is enabled. Existing health checks cover
IPv4 forwarding and do not verify IPv6 connectivity.

To disable, set `experimental_ipv6: false`, restart and reconnect clients. The
saved IPv6 prefix is ignored while disabled and no IPv6 server directive is
rendered. No host IPv6 settings or firewall rules are changed by this feature.

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
