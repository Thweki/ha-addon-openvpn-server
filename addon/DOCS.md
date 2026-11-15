# OpenVPN Server Add-on Docs

Full repository documentation:

- [README.md](https://github.com/Thweki/ha-addon-openvpn-server/blob/main/README.md)
- [README.zh-CN.md](https://github.com/Thweki/ha-addon-openvpn-server/blob/main/README.zh-CN.md)

## Experimental client retry policy

For devices on unreliable connections, configure reconnect pacing and optionally
stop retrying after a finite number of attempts. This feature defaults **OFF**;
with `experimental_client_retry: false`, exported profiles keep their existing
behavior. To try a five-second initial delay, a one-minute backoff cap, and twelve
attempts per remote, add these options:

```yaml
experimental_client_retry: true
client_auth_retry: none
client_connect_retry_seconds: 5
client_connect_retry_max_seconds: 60
client_connect_retry_max_attempts: 12
```

| Option | Default | Meaning when enabled |
| --- | --- | --- |
| `client_auth_retry` | `none` | `none`: stop on authentication failure; `nointeract`: retry without prompting; `interact`: prompt again before retrying. |
| `client_connect_retry_seconds` | `1` | Initial delay between connection attempts, 1–86400 seconds. |
| `client_connect_retry_max_seconds` | `300` | Backoff cap, 1–86400 seconds and at least the initial delay. |
| `client_connect_retry_max_attempts` | `0` | Attempts per remote, 1–65535; 0 omits the limit (unlimited). A successful connection resets the counter. |

OpenVPN increases the delay after repeated failures. The cap is the second argument
of `connect-retry`; the attempt count is the separate `connect-retry-max` directive.
The attempt count is not an overall wall-clock timeout: DNS resolution still uses
`resolv-retry infinite` and connection attempts can take additional time.

Save the options and restart the add-on, then download each profile again from the
panel (or run `openvpn-addon client export NAME`) and re-import it on the device.
Previously imported profiles and stored exports for other clients are not updated
automatically. To undo, disable the flag and repeat the export/import steps.

These directives target OpenVPN 2.x clients, for both UDP and TCP. Other clients
may ignore them or apply their own reconnect policy; check your client's log.
Auth retry handles `AUTH_FAILED`/password errors, not expired or revoked certificates,
and does not add password authentication to this certificate-based server.
See the [OpenVPN 2.6 manual](https://openvpn.net/community-docs/community-articles/openvpn-2-6-manual/)
for client retry semantics. The policy only changes newly rendered client profiles;
it does not change server keepalive, connection rate limiting, or supervision.

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
