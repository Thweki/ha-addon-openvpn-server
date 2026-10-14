# Home Assistant OpenVPN Server Add-on

英文主文档： [README.md](README.md)

这是一个运行在 `Home Assistant OS` 上的 OpenVPN 服务端 Add-on。

## 能力概览

- 在 `Home Assistant OS` 上运行 OpenVPN 服务端，使用 EasyRSA 管理 PKI。
- 通过路由推送 + NAT 让 VPN 客户端访问家庭内网。
- 自带插件面板（Home Assistant ingress），用于客户端管理、健康检查与诊断。
- 客户端签发与吊销支持三种入口：面板、命令行、`/share` 请求文件。

## 安装

1. 打开 `设置 -> 加载项 -> 加载项商店 -> 仓库`。
2. 添加仓库地址：

```text
https://github.com/Thweki/ha-addon-openvpn-server
```

3. 安装 `OpenVPN Server`。
4. 配置并启动 Add-on。

## 基础配置示例

```yaml
public_endpoint: vpn.example.com
listen_port: 1194
public_port: 443
protocol: udp
lan_networks: 192.168.1.0/24
nat_enabled: true
dns: 1.1.1.1, 8.8.8.8
extra_routes: ""
```

字段含义：

- `public_endpoint`：客户端连接使用的公网域名或公网 IP
- `listen_port`：Home Assistant 本机监听端口
- `public_port`：写入客户端配置文件的外部端口
- `lan_networks`：客户端需要访问的内网网段，多个值用逗号分隔
- `nat_enabled`：客户端访问内网设备必须开启
- `dns`：推送给客户端的 DNS，多个值用逗号分隔
- `extra_routes`：额外推送路由，多个值用逗号分隔

## 插件面板

`设置 -> 加载项 -> OpenVPN Server -> 打开 Web UI`（仅管理员可见）：

- **健康**：内网转发/NAT、`ip_forward`、吊销列表有效期、端口映射、配置告警
- **客户端**：签发、吊销、查看在线状态、下载 `.ovpn`
- **维护**：重建吊销列表、重启 OpenVPN、下载诊断包

## 客户端证书的两种模式

由 `duplicate_cn` 选择：

| `duplicate_cn` | 行为 |
| --- | --- |
| `false`（推荐） | 每台设备一份证书；每个客户端固定一个 VPN 地址（`fixed_client_ips`），路由器与防火墙规则稳定，吊销某台不影响其它设备。 |
| `true` | 一份配置可被多台设备同时使用；吊销该证书会同时登出所有设备，且固定地址功能自动失效。 |

签发与吊销既可在面板操作，也可用命令行：

```bash
openvpn-addon client create iphone
openvpn-addon client revoke iphone
openvpn-addon client list
```

## 客户端配置

导出的配置内含客户端私钥，文件权限为 `0600`，请从面板下载。

把 `publish_profiles_to_share` 设为 `true` 会额外复制到 `/share/openvpn_server/clients/`；该目录可被其它插件读取，非必要不建议开启。

## 请求文件（可选，便于脚本化）

把文件写入 `/share/openvpn_server/requests/` 即可，**无需重启插件、不会断开在线客户端**：

```json
{"action":"issue","client_name":"iphone"}
```

```json
{"action":"revoke","client_name":"iphone"}
```

输出：

- 处理结果：`/share/openvpn_server/results/`
- 已处理请求：`/share/openvpn_server/archive/`
- 无效请求：`/share/openvpn_server/rejected/`（附说明原因的结果文件）

格式错误的请求会被隔离，不会让插件启动失败。

## 说明

- 如果 `public_endpoint` 为空，Add-on 不会导出客户端配置。
- 如果 `public_port` 和 `listen_port` 不同，需要在路由器或防火墙上完成端口转发。
- 如果 `nat_enabled` 关闭，即使填了 `lan_networks`，客户端也只能访问 VPN 服务端本身。
- 如果 `lan_networks` 为空，客户端不会自动获得家庭内网路由。
- `dns`、`lan_networks`、`extra_routes` 支持逗号分隔的多值。
- 优先使用 `protocol: udp`；只有网络封锁 UDP 时才用 TCP。
- 排障见 [addon/DOCS.md](addon/DOCS.md)，或使用面板的「下载诊断包」。
- 变更历史：[addon/CHANGELOG.md](addon/CHANGELOG.md)。
