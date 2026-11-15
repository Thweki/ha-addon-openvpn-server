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

## Experimental: automatic CRL renewal

An expired certificate revocation list (CRL) prevents new VPN connections even when
client certificates are still valid. To enable automatic maintenance, set this add-on
option in Home Assistant and restart the add-on:

```yaml
experimental_crl_auto_renew: true
```

This option defaults to `false`. When enabled, the running add-on checks the CRL
within five seconds of starting its request watcher and then hourly. It rebuilds
the CRL when missing, expired, or within seven days of expiry. For a configured
`crl_valid_days` below 14, the renewal window is half that lifetime instead, avoiding
renewal on every check. New CRLs use the existing `crl_valid_days` option.

This renews the **revocation list only**, not client/server certificates or keys.
Existing revocations remain in effect. Client profiles need no redistribution, and
OpenVPN need not restart: it rereads the CRL on connections and TLS renegotiation
([OpenVPN documentation](https://openvpn.net/community-docs/revoking-certificates/)).

Automatic signing uses EasyRSA's temporary-file publication and serializes with
other add-on PKI commands while enabled. Do not run EasyRSA directly against the
same PKI during maintenance. Missing PKI indexes and unreadable/malformed CRLs
produce a warning instead of resetting PKI state. A failed check/signing attempt
retries after one hour; monitor **Health** and the add-on log. Subprocess output is
suppressed, and renewal logs never contain private keys. This requires the existing
unattended CA key setup; an encrypted CA key cannot prompt for a passphrase.

To disable, set the option to `false` and restart. The default behavior (startup
and manual CRL rebuilds only) remains unchanged. Keep regular PKI backups before
trying this experimental feature.

Focused tests: `PYTHONPATH=addon/src python3 -m unittest discover -s tests -p test_crl_renewal.py -v`.
The real signing test uses EasyRSA from PATH or `/usr/share/easy-rsa/easyrsa`;
set `EASYRSA_TEST_BINARY` to test another installed binary. It creates a temporary
CA, verifies that revoked clients remain rejected and valid clients still verify,
and checks that signing failure preserves the previous CRL. Without EasyRSA/OpenSSL,
that integration test skips; the remaining tests require only Python.

## Files and paths

- Client profiles: `/data/openvpn/exports/` (mode `0600`)
- Optional `/share` copies: `/share/openvpn_server/clients/` (only with `publish_profiles_to_share`)
- Requests and results: `/share/openvpn_server/requests/`, `results/`, `archive/`, `rejected/`
- PKI: `/data/openvpn/pki/`
- Runtime: `/data/openvpn/runtime/` (`server.conf`, OpenVPN status, per-client `ccd/`)
