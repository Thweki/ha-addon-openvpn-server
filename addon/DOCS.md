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

## Experimental client identity verification

Restrict connections to a named device or a namespace of device certificates, even
when other certificates were issued by the same CA. This is disabled by default:

```yaml
experimental_client_verify: false
client_verify_mode: exact
client_verify_name: ""
```

For example, issue certificates named `home-phone` and `home-laptop` using the
panel or `openvpn-addon client create home-phone`, then enable:

```yaml
experimental_client_verify: true
client_verify_mode: prefix
client_verify_name: "home-"
```

Save the options and restart the add-on. The restart disconnects existing sessions;
clients reconnect under the new policy. `home-phone` and `home-laptop` match, while
`guest-phone` does not. For one certificate, use `exact` and its full client name.
Matches are case-sensitive. Prefixes are literal, not wildcard or regular expressions;
use a delimiter such as `home-` to avoid unintentionally matching `homeguest`.

The name must be 1–63 ASCII letters, digits, underscores, dots or dashes and start
with a letter or digit. An enabled policy with an empty name or invalid mode/name
fails configuration validation. With the flag off, the policy fields are ignored.
Set the flag back to `false` and restart to remove the restriction.

The server uses OpenVPN's
[`verify-x509-name`](https://openvpn.net/community-docs/community-articles/openvpn-2-6-manual/)
to check the connecting client's certificate Common Name (CN); this does not check
SANs or the server's hostname. CA trust, client certificate purpose and CRL checks
still apply. Use prefix mode only with a CA whose certificate issuance you control.
Matching a name never overrides revocation or expiry.

Issuance and profile exports remain available for nonmatching names, but those
certificates cannot connect while the policy is enabled. This includes the default
`homeassistant` certificate in the example above. Existing matching profiles need
no re-export, and fixed IPs and `duplicate_cn` keep their existing meaning.

## Diagnostics

The panel's **Download diagnostics** produces a text bundle with the status payload, runtime details and the rendered `server.conf`. Attach it to bug reports; it does not contain private keys.

## Files and paths

- Client profiles: `/data/openvpn/exports/` (mode `0600`)
- Optional `/share` copies: `/share/openvpn_server/clients/` (only with `publish_profiles_to_share`)
- Requests and results: `/share/openvpn_server/requests/`, `results/`, `archive/`, `rejected/`
- PKI: `/data/openvpn/pki/`
- Runtime: `/data/openvpn/runtime/` (`server.conf`, OpenVPN status, per-client `ccd/`)
