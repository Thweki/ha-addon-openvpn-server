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

## Experimental UDP packet sizing

For UDP tunnels that connect but stall during large transfers, the add-on can
apply matching `fragment` and `mssfix` settings to the server and exported client
profiles. This feature defaults **OFF** and does not change the tunnel MTU.

For example, to limit TCP packet sizes inside the tunnel:

```yaml
protocol: udp
experimental_packet_sizing: true
fragment_size: 0
mssfix_size: 1300
```

- `fragment_size`: `0` disables OpenVPN fragmentation. Set `576`–`1500` to split
  oversized data packets inside OpenVPN, including non-TCP traffic.
- `mssfix_size`: `0` uses OpenVPN's automatic behavior, inheriting `fragment_size`
  when set. Set `576`–`1500` to limit encapsulated TCP packet sizes explicitly.
  Here `0` means automatic, **not** OpenVPN's `mssfix 0` (which disables MSS fixing).
- Sizes include outer IP/UDP headers (`mtu` syntax), not the TCP MSS value itself.
  The range is deliberately limited to ordinary links up to 1500 bytes. `1300` is
  an example to test, not a measured path MTU. See the
  [OpenVPN 2.6 manual](https://openvpn.net/community-docs/community-articles/openvpn-2-6-manual/)
  for packet sizing semantics.
- Nonzero sizes require UDP while the flag is on. With the flag off, stored sizes
  have no effect. With both sizes at `0`, no directives are added.

Active sizing disables data channel offload (DCO) on both peers so packet sizing
runs in userspace; throughput may decrease. Use OpenVPN Community 2.6+ clients.
**OpenVPN Connect does not support fragmentation** and cannot connect to a server
with `fragment_size` enabled; see its
[unsupported directives](https://openvpn.net/connect-docs/directives-not-supported/).
Only enable fragmentation if every client supports it. To combine fragmentation
and MSS fixing, set `fragment_size: 1300` and leave `mssfix_size: 0`, or set a
separate MSS packet size.

After any change, restart the add-on, download fresh profiles for **every** client
from the panel (or run `openvpn-addon client export NAME`), and re-import them on
the devices. These settings are embedded in profiles, not pushed to existing
clients. Previously downloaded profiles and non-default exports on disk are not
automatically updated. To roll back, set `experimental_packet_sizing: false`,
restart, then re-export and re-import all profiles again. Certificates need not
be reissued.

## Diagnostics

The panel's **Download diagnostics** produces a text bundle with the status payload, runtime details and the rendered `server.conf`. Attach it to bug reports; it does not contain private keys.

## Files and paths

- Client profiles: `/data/openvpn/exports/` (mode `0600`)
- Optional `/share` copies: `/share/openvpn_server/clients/` (only with `publish_profiles_to_share`)
- Requests and results: `/share/openvpn_server/requests/`, `results/`, `archive/`, `rejected/`
- PKI: `/data/openvpn/pki/`
- Runtime: `/data/openvpn/runtime/` (`server.conf`, OpenVPN status, per-client `ccd/`)
