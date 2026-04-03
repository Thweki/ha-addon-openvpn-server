# Home Assistant OpenVPN Server Add-on

英文主文档： [README.md](README.md)

这是一个运行在 `Home Assistant OS` 上的 OpenVPN 服务端 Add-on。

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
dns: 1.1.1.1, 8.8.8.8
extra_routes: ""
```

字段含义：

- `public_endpoint`：客户端连接使用的公网域名或公网 IP
- `listen_port`：Home Assistant 本机监听端口
- `public_port`：写入客户端配置文件的外部端口
- `lan_networks`：客户端需要访问的内网网段，多个值用逗号分隔
- `dns`：推送给客户端的 DNS，多个值用逗号分隔
- `extra_routes`：额外推送路由，多个值用逗号分隔

## 客户端签发与吊销

默认客户端配置文件会导出到：

- `/share/openvpn_server/clients/homeassistant.ovpn`

签发客户端：

```json
{"action":"issue","client_name":"iphone"}
```

将上面的内容写入：

- `/share/openvpn_server/requests/iphone.json`

吊销客户端：

```json
{"action":"revoke","client_name":"iphone"}
```

结果目录：

- 配置文件：`/share/openvpn_server/clients/`
- 处理结果：`/share/openvpn_server/results/`
- 请求归档：`/share/openvpn_server/archive/`

## 说明

- 如果 `public_endpoint` 为空，Add-on 不会导出客户端配置。
- 如果 `public_port` 和 `listen_port` 不同，需要在路由器或防火墙上完成端口转发。
- 如果 `lan_networks` 为空，客户端不会自动获得家庭内网路由。
