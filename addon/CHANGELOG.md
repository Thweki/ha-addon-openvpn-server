# Changelog

## 0.2.0

### Added

- Built-in add-on panel (Home Assistant ingress, admin only): online clients, one-click issue/revoke, `.ovpn` download, health checks, CRL rebuild, OpenVPN restart and a downloadable diagnostics bundle.
- Health self-check surfaced in the panel and in `status`: LAN forwarding/NAT rules, `ip_forward`, CRL validity, port mapping and configuration warnings.
- Explicit warning when `nat_enabled` is off while `lan_networks` is set — the misconfiguration that silently removes LAN access.
- `fixed_client_ips`: every client gets a stable VPN address through `client-config-dir` when certificates are not shared.
- `crl_valid_days`: the CRL lifetime is now explicit (default 3650 days) and shown with its remaining days.
- `publish_profiles_to_share`: `/share` export is now opt-in.
- Request files in `/share/openvpn_server/requests/` are processed while the add-on runs (no restart needed) and invalid files are quarantined into `rejected/` with an explanatory result instead of stopping the add-on.

### Changed

- OpenVPN now runs as a supervised child process with restart backoff, so a bad configuration no longer turns into a silent crash loop.
- NAT rules live in a dedicated `OPENVPN_ADDON` chain that is reconciled on start and removed on shutdown, instead of appending rules to `POSTROUTING`/`FORWARD` forever.
- Network setup failures are reported and no longer prevent the add-on (and its panel) from starting.
- Modern crypto directives: `data-ciphers`/`data-ciphers-fallback` replace `cipher`, `remote-cert-tls client` is enforced, `tls-version-min 1.2`, and the deprecated `ecdh-curve` is gone.
- `client-config-dir` is only emitted when per-device certificates are used, removing the `duplicate-cn`/`client-config-dir` contradiction.
- Unknown option keys are ignored with a warning instead of breaking startup, and scalar values from a hand-edited `options.json` are coerced.
- Revoked clients are no longer silently re-exported as revoked; issuing an existing revoked name reports what happened.

### Security

- Exported client profiles are written with mode `0600` and no longer copied to `/share` unless explicitly enabled, because they embed the client private key and `/share` is readable by other add-ons.

### Removed

- Unused top-level duplicate modules in the local add-on folder.
