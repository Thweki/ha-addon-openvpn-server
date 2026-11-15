# OpenVPN Server Add-on Docs

Full repository documentation:

- [README.md](https://github.com/Thweki/ha-addon-openvpn-server/blob/main/README.md)
- [README.zh-CN.md](https://github.com/Thweki/ha-addon-openvpn-server/blob/main/README.zh-CN.md)

## Experimental client connection notifications

Receive a JSON webhook when OpenVPN calls its client connect/disconnect hooks, for
example to trigger a Home Assistant automation when a named device connects.
This feature is **off by default** and independent of status-file polling.

Set these add-on options, then restart the add-on:

```yaml
experimental_client_notifications: true
client_notification_url: "https://your-receiver.example/api/webhook/your-webhook-id"
```

The receiver must accept HTTP `POST` with `Content-Type: application/json` and
return a `2xx` status. For Home Assistant, use a webhook automation that accepts
POST; its JSON body is available as `trigger.json`. Only send to a receiver you
trust with client names and VPN addresses. HTTPS uses normal certificate
verification; HTTP is also supported for trusted local receivers. URL userinfo
(`user:password@host`) and fragments are rejected. Treat a secret webhook path or
query as a credential in your add-on options. No additional authentication headers
are sent. Redirects and environment proxies are not followed.

Example payload (the same fields are sent for both events):

```json
{
  "schema_version": 1,
  "event": "client-connect",
  "client_name": "laptop",
  "vpn_ip": "10.8.0.2",
  "occurred_at": "2026-11-15T12:00:00+00:00"
}
```

`event` is `client-connect` or `client-disconnect`; `occurred_at` is the UTC hook
time, and `vpn_ip` is null if OpenVPN does not supply it. Payloads contain only
these five fields: no keys, certificates, profiles, public peer addresses, or
environment dump. The destination URL is never put in `server.conf` or notification
log messages.

Delivery is **best effort**, with one attempt per event and no persistent history
or retries. The synchronous hook only sends a nonblocking local datagram to a
mode-`0600` Unix socket; a single background worker performs HTTP delivery with a
three-second socket timeout. The bounded kernel queue drops new events when full.
A failed receiver, missing worker, or full queue does not reject VPN clients.
Shutdown/crashes can lose pending events, and disconnect detection depends on
OpenVPN's keepalive behavior. A connect hook is not proof the client finished
configuring its tunnel. Shared certificates can produce multiple events with the
same client name; this is not a reliable presence database or audit log.

To disable, set `experimental_client_notifications: false` and restart. This
removes the hooks and starts no notification worker, even if a URL remains set.
Hook semantics follow the [OpenVPN manual](https://openvpn.net/community-docs/community-articles/openvpn-2-6-manual/).

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
