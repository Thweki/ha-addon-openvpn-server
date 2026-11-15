# OpenVPN Server Add-on Docs

Full repository documentation:

- [README.md](https://github.com/Thweki/ha-addon-openvpn-server/blob/main/README.md)
- [README.zh-CN.md](https://github.com/Thweki/ha-addon-openvpn-server/blob/main/README.zh-CN.md)

## Experimental: networks behind a client router

Use per-client CCD overrides when Home Assistant needs to reach a remote subnet
behind an OpenVPN client, such as sensors at a cabin. The feature is **off by
default**. Configure it in the add-on options (YAML mode):

```yaml
experimental_client_overrides: true
fixed_client_ips: true
duplicate_cn: false
device: tun
client_overrides:
  - client_name: cabin-router
    iroutes: "192.168.50.0/24, 192.168.51.10/32"
```

Issue `cabin-router` through the existing client panel and install that profile on
the remote router. `client_name` must exactly match its certificate name. These
options do not create or re-enable certificates. Restart the **add-on** after
changing options; restarting only the OpenVPN process does not reload options.

For each network, the add-on writes a server `route` and an `iroute` in that
client's generated CCD file, alongside its existing automatic `ifconfig-push`.
The server route sends traffic into the VPN; the CCD route assigns the network
to its client. See the [OpenVPN manual](https://openvpn.net/community-docs/community-articles/openvpn-2-6-manual/)
for `iroute` and `client-config-dir` semantics.

- Requires `tun`, fixed client addresses, and one certificate per device.
- `iroutes` is a comma-separated string of IPv4 networks with explicit prefixes
  and no host bits (individual hosts use `/32`). IPv6 and arbitrary CCD directives
  are not supported. `DEFAULT` and `server` are reserved client names.
- Duplicate client entries, overlapping client networks, default routes, and
  overlaps with the VPN subnet or `lan_networks` are rejected before files change.
- Configure forwarding and firewall access on the remote router, and a return
  route from its LAN to the source network used by Home Assistant (or NAT on that
  router). This feature does not install site-to-site firewall/NAT rules. Access
  from other home LAN hosts also needs a route via the VPN server and host forwarding.
- Routes are not automatically advertised to other VPN clients. If those clients
  should reach the remote network, also add it to `extra_routes` and enable
  `client_to_client`. OpenVPN suppresses a matching pushed route for its owning
  client. Do not put remote networks in `lan_networks`, which describes the server LAN.
- Only valid, issued clients receive CCD files; revocation removes the file during
  the existing client synchronization. Server routes remain configured while a
  client is absent, revoked, or offline, so traffic to that network cannot be
  delivered until its owner connects. Remove the override and restart to remove
  the server route.
- Removing an override or setting `experimental_client_overrides: false`, then
  restarting the add-on, removes generated overrides while retaining ordinary
  fixed VPN addresses. Disabled overrides are ignored and may be kept for later.
  Do not edit generated CCD files directly; synchronization replaces them.

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
