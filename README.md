# Home Assistant OpenVPN Server Add-on

OpenVPN server add-on for `Home Assistant OS`.

This add-on provides:

- OpenVPN server bootstrap and runtime management
- PKI initialization with EasyRSA
- Single-device client certificate issuing
- Client profile export to the Home Assistant share folder
- LAN route push and NAT support for home network access

## Installation

1. In Home Assistant, go to `Settings -> Add-ons -> Add-on Store -> Repositories`.
2. Add this repository URL:

```text
https://github.com/Thweki/ha-addon-openvpn-server
```

3. Install `OpenVPN Server`.
4. Configure the add-on.
5. Start the add-on.

## Basic Configuration

Recommended example:

```yaml
public_endpoint: vpn.example.com
listen_port: 1194
public_port: 443
protocol: udp
lan_networks: 192.168.1.0/24
dns: 1.1.1.1, 8.8.8.8
extra_routes: ""
```

Field summary:

- `public_endpoint`: public domain or IP used by clients
- `listen_port`: local port OpenVPN listens on
- `public_port`: external port written into exported `.ovpn` files
- `lan_networks`: LAN subnets clients should reach, comma-separated
- `dns`: DNS servers pushed to clients, comma-separated
- `extra_routes`: optional extra pushed routes, comma-separated

## Client Profiles

Default client profile:

- exported to `/share/openvpn_server/clients/homeassistant.ovpn`

Issue a new client:

- create `/share/openvpn_server/requests/<name>.json`
- request body:

```json
{"action":"issue","client_name":"iphone"}
```

Revoke a client:

- create `/share/openvpn_server/requests/<name>.json`
- request body:

```json
{"action":"revoke","client_name":"iphone"}
```

Request processing outputs:

- profiles: `/share/openvpn_server/clients/`
- results: `/share/openvpn_server/results/`
- archive: `/share/openvpn_server/archive/`

## Notes

- If `public_endpoint` is empty, client export is skipped.
- If `public_port` differs from `listen_port`, your router or firewall must forward the external port to the Home Assistant host.
- If `lan_networks` is empty, clients will not automatically receive home LAN routes.
- `dns`, `lan_networks`, and `extra_routes` accept comma-separated values.
