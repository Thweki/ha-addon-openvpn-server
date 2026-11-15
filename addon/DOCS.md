# OpenVPN Server Add-on Docs

Full repository documentation:

- [README.md](https://github.com/Thweki/ha-addon-openvpn-server/blob/main/README.md)
- [README.zh-CN.md](https://github.com/Thweki/ha-addon-openvpn-server/blob/main/README.zh-CN.md)

## Experimental: per-client idle policies

Disconnect an occasional-use laptop after inactivity while leaving an always-on
bridge connected. This is separate from the existing global `inactive_timeout`:
clients without a policy continue to use that setting. The experiment defaults
**off** and does not change server configuration, client profiles, or session
behavior while disabled.

To enable it, add the following in the add-on Configuration YAML, substituting
your client certificate names, then save and restart the add-on:

```yaml
experimental_client_idle: true
fixed_client_ips: true
duplicate_cn: false
client_idle_policies:
  - client: laptop
    timeout: 900
  - client: phone
    timeout: 600
    min_bytes: 1024
  - client: bridge
    timeout: 0
```

- `timeout` is in seconds. Without `min_bytes` (or with `min_bytes: 0`), any
  incoming or outgoing tunnel packet resets the idle timer.
- With positive `min_bytes`, OpenVPN disconnects the client if combined tunnel
  traffic is below that byte threshold during the timeout interval. This can
  disconnect devices that generate only background traffic; start conservatively.
- `timeout: 0` disables the client's idle timer, including an inherited global
  `inactive_timeout`. It cannot be combined with positive `min_bytes`.
- OpenVPN keepalive and TLS control packets do not count as activity. These are
  OpenVPN's native [inactive timer semantics](https://build.openvpn.net/man/openvpn-2.6/openvpn.8.html).
- Policies require `fixed_client_ips: true` and `duplicate_cn: false`, and are
  applied through the existing per-client `ccd/` files. They do not push settings
  to client apps or inspect packet contents. The server disconnects the affected
  session; it does not revoke the certificate or prevent automatic reconnection.
- Names must exactly match client certificate names. Duplicate names, reserved
  names (`server`, `DEFAULT`), unknown fields, and invalid numbers are rejected.
  Numeric values must be integers from 0 to 2147483647. A policy for a name not yet
  issued stays dormant; only currently valid clients receive CCD files.

To remove a policy, delete its entry. To disable all policies, set
`experimental_client_idle: false`. Save and restart the add-on in either case;
startup regenerates CCD files and clears the old policy directives. Existing
connections do not reload edited CCD files, and a restart briefly disconnects all
clients. Leave generated files alone and manage policies through configuration.
The existing global `inactive_timeout` is retained when the experiment is disabled.
The panel and CLI configuration warnings identify the experiment when enabled.

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
