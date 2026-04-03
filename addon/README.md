# OpenVPN Server

OpenVPN server add-on for `Home Assistant OS`.

Repository:

- [https://github.com/Thweki/ha-addon-openvpn-server](https://github.com/Thweki/ha-addon-openvpn-server)

Chinese documentation:

- [README.zh-CN.md](../README.zh-CN.md)

## Quick Start

Recommended configuration:

```yaml
public_endpoint: vpn.example.com
listen_port: 1194
public_port: 443
protocol: udp
lan_networks: 192.168.1.0/24
dns: 1.1.1.1, 8.8.8.8
extra_routes: ""
```

## Client Profiles

Default profile:

- `/share/openvpn_server/clients/homeassistant.ovpn`

Issue a client:

```json
{"action":"issue","client_name":"iphone"}
```

Revoke a client:

```json
{"action":"revoke","client_name":"iphone"}
```

Request directory:

- `/share/openvpn_server/requests/`

Result directories:

- `/share/openvpn_server/clients/`
- `/share/openvpn_server/results/`
- `/share/openvpn_server/archive/`

## Notes

- `public_port` may differ from `listen_port` when router port forwarding uses a different external port.
- `lan_networks`, `dns`, and `extra_routes` accept comma-separated values.
