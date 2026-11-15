# OpenVPN Server Add-on Docs

Full repository documentation:

- [README.md](https://github.com/Thweki/ha-addon-openvpn-server/blob/main/README.md)
- [README.zh-CN.md](https://github.com/Thweki/ha-addon-openvpn-server/blob/main/README.zh-CN.md)

## Experimental download bandwidth limit

To keep VPN downloads from saturating your home uplink, enable a shared cap in
the add-on options, then restart the add-on:

```yaml
experimental_bandwidth_limit: true
download_limit_kbps: 10000
```

The flag defaults to `false`; the rate defaults to `10000` (10 Mbit/s).
Accepted rates are 64–1000000 **kilobits per second** (decimal, not bytes).
The cap applies to the combined server-to-client tunnel traffic, including
downloads from Home Assistant, LAN hosts and forwarded internet traffic. It is
not a per-client allocation or fairness guarantee. Client uploads are unlimited.
Encryption/transport overhead is outside the cap, so allow headroom below your
home uplink rate. Short bursts are possible; excess queued traffic is dropped.

Both UDP and TCP are supported. This requires `device: tun` and
`client_to_client: false`. Enabling the feature disables data-channel offload
(DCO), which can reduce peak performance. Existing client profiles need no change.
The implementation uses Linux `tc` TBF on `tun0`, with a burst allowance of at
least 1600 bytes or 10 ms of traffic and a queue latency setting of 100 ms.
The bundled `iproute2` package includes `tc`; the host kernel must support TBF.

If the hook cannot install the cap (for example, missing kernel support or an
existing root qdisc), OpenVPN startup fails and logs `Bandwidth limit setup
failed`. It does not overwrite an existing qdisc or run uncapped. The limit
survives OpenVPN soft restarts with the tunnel; a full process restart removes
the tunnel and its qdisc and installs the configured rate on the new tunnel.
Changing the rate or setting `experimental_bandwidth_limit: false` requires an
add-on restart. Do not alter `tun0` traffic control externally while it is active.

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
