# OpenVPN Server Add-on Docs

Full repository documentation:

- [README.md](https://github.com/Thweki/ha-addon-openvpn-server/blob/main/README.md)
- [README.zh-CN.md](https://github.com/Thweki/ha-addon-openvpn-server/blob/main/README.zh-CN.md)

## Experimental random listen port

For testing router/firewall forwarding with changing local VPN ports, enable:

```yaml
experimental_random_port: true
```

Default: **off**. Each add-on start randomly probes up to 64 distinct ports in
49152–65535 using the configured UDP/TCP protocol, then uses the first available
port instead of `listen_port`. The panel summary and startup log show the selected
port. It is also recorded in `/data/openvpn/runtime/server.conf`; `openvpn-addon
status` reads that last rendered port (not proof that OpenVPN is running). Before
the first start, status shows the configured `listen_port`.

After **every add-on restart**, update your router/firewall destination to the
selected local port. The public port in client profiles (`public_port`, or the
explicit port in `public_endpoint`) stays unchanged, so a stable external port can
continue to serve existing profiles. The add-on does not update your router. This
mode is intended for deployments with port forwarding, not direct connections
to a fixed local port. It is not a security substitute for certificate validation.

Panel **Restart OpenVPN**, revocations, and child-process crash retries keep the
same port. Only a new add-on run selects again; a random choice can repeat a
previous port. Validation, initialization, and profile exports do not choose ports.
The probe cannot reserve the port until OpenVPN binds it; a competing process can
still claim it, in which case the normal OpenVPN diagnostics/retries apply. Probe
errors or exhaustion abort startup rather than silently using a fixed port.

To disable, set `experimental_random_port: false`, restart the add-on, and restore
forwarding to your configured `listen_port`.

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
