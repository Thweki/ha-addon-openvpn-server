# OpenVPN Server Add-on Docs

Full repository documentation:

- [README.md](https://github.com/Thweki/ha-addon-openvpn-server/blob/main/README.md)
- [README.zh-CN.md](https://github.com/Thweki/ha-addon-openvpn-server/blob/main/README.zh-CN.md)

## Experimental client HTTP proxy

For client networks that require an HTTP CONNECT proxy, exported profiles can
connect to your VPN through that proxy. This feature is **off by default** and
supports proxies without authentication. The client app must support OpenVPN's
[`http-proxy` directive](https://openvpn.net/community-docs/community-articles/openvpn-2-6-manual/).

```yaml
protocol: tcp
experimental_client_http_proxy: true
client_http_proxy_host: proxy.example.com
client_http_proxy_port: 8080
```

Use a proxy address reachable from the client network, not from Home Assistant.
The host must be a bare IPv4 address or DNS name, without a scheme, port, path or
credentials. The proxy must allow CONNECT to your `public_endpoint` and
`public_port`. Those fields still identify the VPN server, not the proxy.

Save the options and restart the add-on, then download and re-import each client
profile (or re-export with `openvpn-addon client export NAME`). Changing the
protocol to TCP also requires TCP forwarding on your router and updated profiles
on existing clients. The proxy option applies to every newly rendered profile;
already imported profiles are not updated automatically. Certificate-role
verification and TLS protection remain in place.

To return to direct connections, set `experimental_client_http_proxy: false`,
restart, and download and re-import profiles again. Retained proxy host/port
values have no effect while disabled. This does not configure a proxy for
Home Assistant's own traffic.

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
