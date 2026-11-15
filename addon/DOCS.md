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

## Client address leases

Inside the add-on container, run:

```sh
openvpn-addon client leases
```

This read-only command prints JSON with `leases`, `status`, and `fixed_assignments`.
It reads only the OpenVPN status snapshot and saved `client_ips.json` address map
after loading configuration. It does not read certificates, private keys, or profiles,
run network commands, allocate addresses, or write files. No new option is required.

Each lease includes `name`, `vpn_address`, `source`, `real_address`, and
`connected_since`. A `session` row is an observation from the last status snapshot;
a `fixed` row is a saved assignment, not proof of a connection. Matching saved and
observed addresses appear once as a session. If the addresses differ, both appear
so you can see the discrepancy. Multiple sessions sharing a certificate remain
separate. A session with no matching IP route has `vpn_address: null`.

The reader supports OpenVPN status versions 1, 2, and 3. For the add-on's default
version 1, it matches client and routing tables by name and remote endpoint.
Versions 2/3 supply addresses directly in named client columns. Network routes and
MAC addresses are not treated as client IPs. No DHCP-style lease expiry is inferred.

`status.state` is `fresh`, `stale`, `missing`, `unreadable`, or `invalid`.
`age_seconds` is the status file's modification age, or null when unavailable.
Snapshots older than twice `status_interval` are labelled stale; their observations
are retained. Freshness does not prove that the OpenVPN process is still running.
Incomplete, malformed, or unsupported snapshots produce no session rows; retry
after the next status update. Saved assignments can still be listed independently.

`fixed_assignments` is `ok`, `partial` (invalid entries omitted), `missing`,
`unreadable`, `invalid`, or `disabled`. Retained maps are ignored when fixed IPs are
disabled or shared-certificate mode is enabled. Saved assignments are not checked
against certificates or CCD files. The command returns success even when a source
is unavailable; scripts should inspect the source states before using the rows.

## Files and paths

- Client profiles: `/data/openvpn/exports/` (mode `0600`)
- Optional `/share` copies: `/share/openvpn_server/clients/` (only with `publish_profiles_to_share`)
- Requests and results: `/share/openvpn_server/requests/`, `results/`, `archive/`, `rejected/`
- PKI: `/data/openvpn/pki/`
- Runtime: `/data/openvpn/runtime/` (`server.conf`, OpenVPN status, per-client `ccd/`)
