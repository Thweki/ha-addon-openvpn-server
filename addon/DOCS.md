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

## Cipher report

Inside the add-on container, run:

```sh
openvpn-addon ciphers
```

This read-only JSON report inspects `/data/openvpn/runtime/server.conf` (under `OPENVPN_ADDON_DATA` if overridden). It needs no running OpenVPN process, valid options file, PKI, or external tools. It does not change settings or restart the service.

`settings` contains occurrences of `data-ciphers`, `data-ciphers-fallback`, legacy `cipher`/`ncp-ciphers`, `tls-version-min`/`tls-version-max`, `tls-cipher`, and `tls-ciphersuites`. Each occurrence has its source line and ordered values, with an advisory assessment:

- `modern`: a recognized AEAD cipher/suite or TLS 1.2/1.3 version; not proof of availability or overall security.
- `legacy`: a recognized CBC compatibility choice, not necessarily broken or formally deprecated.
- `deprecated`: a recognized obsolete choice, such as BF-CBC, DES, RC4, TLS 1.0/1.1, or disabled data encryption.
- `unknown`: an unrecognized value or library expression, such as `DEFAULT:!RC4`; expressions are not expanded.

Empty lists mean the directive is not explicitly configured. Library defaults are not guessed. In particular, the shipped template sets data ciphers and a TLS minimum but leaves TLS suite lists unset. `tls-cipher` controls TLS 1.2 and below; `tls-ciphersuites` controls TLS 1.3. These distinctions follow the [OpenVPN 2.6 manual](https://build.openvpn.net/man/openvpn-2.6/openvpn.8.html).

The report describes disk configuration, not negotiated client ciphers or confirmed live state. It preserves repeated directives rather than resolving them. `complete: false` and `warnings` identify malformed values, unterminated inline blocks, includes, or unsupported compatibility controls. `complete: true` only describes this limited scan, not full OpenVPN validation. Referenced files are never followed; inline blocks and unrelated directives are omitted. Missing/unreadable configuration produces `available: false` and exit code 1; a readable report returns 0 even when it contains warnings or deprecated choices. No private keys or client profiles are opened.

## Files and paths

- Client profiles: `/data/openvpn/exports/` (mode `0600`)
- Optional `/share` copies: `/share/openvpn_server/clients/` (only with `publish_profiles_to_share`)
- Requests and results: `/share/openvpn_server/requests/`, `results/`, `archive/`, `rejected/`
- PKI: `/data/openvpn/pki/`
- Runtime: `/data/openvpn/runtime/` (`server.conf`, OpenVPN status, per-client `ccd/`)
