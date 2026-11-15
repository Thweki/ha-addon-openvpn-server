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

## Experimental client profile preflight

Check a saved `.ovpn` export before distributing it, or diagnose a stale copy
after changing the endpoint, certificates, or TLS-crypt key. Inside the add-on
container:

```sh
openvpn-addon client check-profile /data/openvpn/exports/laptop.ovpn --experimental-profile-check
```

The experiment defaults **OFF**. Each invocation requires
`--experimental-profile-check`; without it, the command exits before reading
settings or the profile. No add-on configuration changes or restart are needed.

The JSON report contains six checks:

- `connection_settings`: directives match the current generated profile,
  including endpoint/port, protocol, ciphers, and server authentication.
- `ca_match`: the embedded CA matches the current CA.
- `key_match`: the inline private key is valid and matches the certificate
  (supports RSA and EC).
- `certificate`: the certificate verifies against the current CA, with valid
  dates and TLS client purpose.
- `revocation`: verification also succeeds with the current signed CRL;
  revoked certificates and missing, invalid, or expired CRLs fail.
- `tls_crypt`: the inline static key matches the current server setting.

Exit codes: `0` all checks pass, `1` one or more checks fail, `2` disabled,
unsupported/unreadable input, invalid settings, or checks could not complete.
Check names and remediation messages are fixed; the report does not contain
profile contents, identities, endpoints, paths, or OpenSSL error output.

Only this add-on's inline export format is supported, with a 128 KiB limit and
one unencrypted private key and certificate. Duplicate directives/blocks,
external file references, scripts, additional directives, encrypted keys, and
certificate bundles are rejected. The command never executes profile directives,
contacts the VPN endpoint, writes files, or reads the CA/server private keys.
OpenSSL receives the supplied inline client key through stdin, with a five-second
timeout per operation. Keep the input profile private as usual.

This is a snapshot check against settings and PKI on disk, not a live connection
test or a general OpenVPN profile validator. A settings mismatch means the copy
differs from a fresh export, even if the alternative settings could connect.
It does not establish that OpenVPN has reloaded the current configuration/CRL,
that routing works, or that a particular client application supports the profile.

## Files and paths

- Client profiles: `/data/openvpn/exports/` (mode `0600`)
- Optional `/share` copies: `/share/openvpn_server/clients/` (only with `publish_profiles_to_share`)
- Requests and results: `/share/openvpn_server/requests/`, `results/`, `archive/`, `rejected/`
- PKI: `/data/openvpn/pki/`
- Runtime: `/data/openvpn/runtime/` (`server.conf`, OpenVPN status, per-client `ccd/`)
