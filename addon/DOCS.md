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

## Experimental audit history

Set `experimental_audit_log: true` in the add-on options and restart. It defaults
to `false`. Open **Web UI → Audit history** and select **Latest events** to refresh
or **Older events** to page through the history (100 events per page).

The history records:

- `client_connect` and `client_disconnect` from OpenVPN's authenticated client
  hooks, including short sessions between status-file updates.
- `client_revoke` after EasyRSA revokes a certificate, whether requested from the
  panel, CLI or a `/share` request. The event records the PKI change even if the
  subsequent CRL refresh or server restart fails; it does not confirm enforcement.
- `profile_download` after the panel writes a profile response successfully. This
  does not prove the browser saved it. CLI exports and `/share` copies are outside
  this event's scope.

Each event contains a UTC recording time, event type and client name. No keys,
profile contents, addresses, headers or environment dumps are stored. The newest
1,000 events persist across restarts in `/data/openvpn/audit/events.sqlite3`
(file mode `0600`, directory mode `0700`). The store is shared safely by concurrent
panel requests and hook processes; old rows are removed automatically. It is not
a tamper-proof security ledger, and there is no user attribution.

This is best-effort history: database failures produce a warning without blocking
VPN operations, and lock waits are limited to 250 ms. A crash or forced shutdown
can omit a disconnect event; a successful connect hook is not proof that the
client subsequently passed traffic. Read failures appear as **Audit history
unavailable**. Disabling the option and restarting stops recording and hides old
history without deleting it; re-enabling restores access to retained events.

The ingress-relative `GET api/audit` endpoint returns `enabled`, `events` and
`next_before`. Pass `?before=<next_before>` for the next older page. Event IDs
provide stable pagination while new events arrive, though retention may remove
older pages. The endpoint uses the existing panel access boundary and sends
`Cache-Control: no-store`.

The feature enables `script-security 2` and fixed connect/disconnect commands;
it does not accept custom scripts. Hook semantics follow the
[OpenVPN manual](https://openvpn.net/community-docs/community-articles/openvpn-2-6-manual/).

## Files and paths

- Client profiles: `/data/openvpn/exports/` (mode `0600`)
- Optional `/share` copies: `/share/openvpn_server/clients/` (only with `publish_profiles_to_share`)
- Requests and results: `/share/openvpn_server/requests/`, `results/`, `archive/`, `rejected/`
- PKI: `/data/openvpn/pki/`
- Runtime: `/data/openvpn/runtime/` (`server.conf`, OpenVPN status, per-client `ccd/`)
