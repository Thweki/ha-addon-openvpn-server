# Home Assistant OpenVPN Server Add-on 配置界面优化方案[20260403-2002已采用]

## 1. 背景

当前 Add-on 已具备基础可用性，但配置项在 Home Assistant Add-on 配置页中仍然偏底层实现视角，存在以下问题：

- `server_host`、`public_endpoint`、`listen_port`、`public_port` 的语义容易混淆。
- `local_networks` 与 `push_routes` 都与“路由”有关，但定位不同，普通用户难以判断该填哪个。
- 网络、安全、高级项混排，首屏认知负担偏高。
- 配置页缺少“最终效果摘要”，用户保存前难以确认导出的 `.ovpn` 会长什么样。
- 客户端签发与导出能力已经可用，但配置页没有给出足够明确的操作提示。

本方案目标不是新增底层 VPN 能力，而是优化配置界面的信息架构、字段命名、输入提示和运行状态反馈，降低误配率。

## 2. 目标

- 让首次配置用户能在不查文档的情况下完成基础连通配置。
- 明确区分“本地监听参数”和“客户端导出参数”。
- 让“家庭内网访问”配置路径清晰可见。
- 降低高级选项对普通用户的干扰。
- 为后续增加客户端管理 UI 预留一致的信息架构。

## 3. 非目标

- 首版不引入独立 Web UI。
- 不实现 Home Assistant 前端自定义面板。
- 不在本轮中改变 OpenVPN 运行逻辑、证书模型或 NAT 实现方式。
- 不在本轮中把客户端管理从 `/share` 请求机制迁移为完整图形化管理。

## 4. 现状问题拆解

### 4.1 字段语义不直观

- `server_host` 实际是绑定地址，但用户容易误认为公网地址。
- `public_endpoint` 才是客户端真正连接的地址，但当前名称偏技术实现，不够直接。
- `listen_port` 和 `public_port` 的设计已经正确，但如果没有文案解释，用户仍然不知道什么时候需要区分。

### 4.2 常用路径与高级路径没有分层

- 绝大多数家庭用户只需要配置一个公网地址、一个监听端口和一个家庭内网网段。
- `push_routes`、`duplicate_cn`、`client_to_client`、`inactive_timeout` 等字段属于高级项，不应该和基础连接项混排。

### 4.3 缺少“结果导向”的反馈

- 用户配置完成后，最关心的是：
  - 服务端会监听什么地址和端口。
  - 客户端配置会连到哪个地址和端口。
  - 连接后是否能访问家庭内网。
- 当前配置页没有把这三个结果直接总结出来。

## 5. 优化原则

- 优先解释“用户目标”，而不是直接暴露底层实现术语。
- 基础配置默认展开，高级配置默认折叠。
- 尽可能用示例值、帮助文案和摘要减少外部文档依赖。
- 不引入与当前 Add-on schema 明显不兼容的复杂交互。

## 6. 建议配置分组

建议将配置项分为以下 5 组。

### 6.1 基础连接

- `public_endpoint`
- `listen_port`
- `public_port`
- `protocol`

界面文案建议：

- `public_endpoint`：`公网地址或域名`
- `listen_port`：`本地监听端口`
- `public_port`：`客户端连接端口`
- `protocol`：`协议`

帮助文案建议：

- `public_endpoint`：客户端配置文件将连接到此地址，例如 `vpn.example.com` 或公网 IP。
- `listen_port`：OpenVPN 服务在 Home Assistant 主机上实际监听的端口。
- `public_port`：写入客户端配置文件的端口。如果路由器把外网 `443` 转发到本机 `1194`，这里应填写 `443`。

### 6.2 家庭内网访问

- `lan_networks`
- `nat_enabled`
- `redirect_gateway`

界面文案建议：

- `lan_networks`：`家庭内网网段`
- `nat_enabled`：`启用内网访问转发`
- `redirect_gateway`：`让客户端全部流量走 VPN`

帮助文案建议：

- `lan_networks`：填写客户端连接后需要访问的内网段，多个网段用逗号分隔，例如 `192.168.1.0/24, 192.168.2.0/24`。
- `nat_enabled`：启用后，Add-on 会尝试配置转发和 NAT 规则。
- `redirect_gateway`：启用后，客户端的互联网流量也会通过此 VPN。

### 6.3 VPN 网络

- `vpn_subnet`
- `vpn_netmask`
- `dns`
- `device`

此分组面向中等熟练度用户，默认展开但放在基础连接之后。

### 6.4 安全设置

- `cipher`
- `auth`
- `tls_crypt`
- `client_to_client`
- `duplicate_cn`

其中 `duplicate_cn` 和 `client_to_client` 建议增加明显提示：

- `duplicate_cn`：允许多个设备复用同一客户端证书，不推荐开启。
- `client_to_client`：允许 VPN 客户端之间互通，家庭场景通常保持关闭。

### 6.5 高级设置

- `server_host`
- `extra_routes`
- `inactive_timeout`
- `keepalive`
- `log_level`
- `status_interval`
- `client_name`
- `client_expire_days`

此组建议默认折叠。

## 7. 字段命名与文案优化建议

考虑到 Home Assistant Add-on 配置 schema 本身对字段名较固定，本轮建议分两层处理：

### 7.1 内部字段名保持稳定

继续保留当前核心字段名：

- `server_host`
- `public_endpoint`
- `listen_port`
- `public_port`

对多值输入项调整为更适合 HA 原生表单的字符串字段：

- `dns`
- `lan_networks`
- `extra_routes`

原因：

- 已与现有代码、导出模板和远端实例兼容。
- 避免仅因命名调整引入迁移成本。

### 7.2 对外显示文案采用用户语言

如果后续增加 `translations` 或更友好的配置说明，建议显示为：

- `server_host` -> `本地绑定地址`
- `public_endpoint` -> `公网地址或域名`
- `listen_port` -> `本地监听端口`
- `public_port` -> `客户端连接端口`

## 8. 关键校验与提示

建议补充以下校验与提示逻辑。

### 8.1 必填校验

- `public_endpoint` 为空时，不应默默导出客户端配置。
- `lan_networks` 为空且 `nat_enabled=true` 时，应提示“客户端将只能访问 VPN 服务端，不会自动获得家庭内网路由”。

### 8.2 冲突校验

- `vpn_subnet` 不应与任一 `lan_networks` 网段重叠。
- `public_port` 与 `listen_port` 需在合法端口范围内。
- `public_endpoint` 如果填写为内网 IP，应提示这通常只适用于局域网测试。

### 8.3 风险提示

- `redirect_gateway=true` 时，提示“客户端所有互联网流量都会经过此 VPN”。
- `duplicate_cn=true` 时，提示“吊销单一证书时会影响所有复用该证书的设备”。

## 9. 配置结果摘要

建议在配置页底部或保存后日志中提供一段统一摘要。

建议展示内容：

- `服务监听：0.0.0.0:1194/udp`
- `客户端连接：vpn.example.com:443/udp`
- `推送家庭内网：192.168.1.0/24`
- `全流量代理：关闭`

如果短期内无法在 UI 中直接展示，也建议在启动日志中固定输出同等语义的信息，并保持文案稳定，便于用户排查。

## 10. 客户端管理提示优化

当前客户端签发流程已经可用，但缺少面向普通用户的入口说明。

建议在 README、配置说明或 Add-on 描述中明确加入：

- 客户端请求目录：`/share/openvpn_server/requests/`
- 客户端导出目录：`/share/openvpn_server/clients/`
- 请求示例：

```json
{"action":"issue","client_name":"iphone"}
```

- 结果文件目录：`/share/openvpn_server/results/`

如果后续要做轻量 UI，建议优先做：

- 新建客户端
- 查看已签发客户端
- 下载客户端配置
- 吊销客户端

## 11. 分阶段落地建议

### 阶段 1：低风险优化

- 调整配置说明文案。
- 优化 README 使用说明。
- 统一启动日志中的摘要输出。
- 增加输入示例和常见误配说明。

### 阶段 2：配置 schema 体验增强

- 根据 Home Assistant Add-on 能力补充更细的字段说明和默认值策略。
- 调整字段排序，让基础连接与家庭内网访问优先展示。
- 将配置结果摘要同步暴露到 `status` 输出，避免用户必须翻查容器日志。

当前已落地：

- 阶段 1 已完成。
- 阶段 2 已完成字段排序调整，并将摘要与告警输出纳入 `status` 结果。
- 多值输入已从数组标签控件调整为字符串字段 `dns`、`lan_networks`、`extra_routes`，同时保留旧数组配置的运行时兼容解析。

### 阶段 3：客户端管理增强

- 引入更清晰的客户端请求说明。
- 如有必要，增加命令封装或轻量接口，让用户无需直接编写 JSON。

当前已落地：

- `/share/openvpn_server/requests/` 已支持 `issue` 与 `revoke` 两种动作。
- 吊销客户端后会同步清理 `/data/openvpn/exports/` 与 `/share/openvpn_server/clients/` 中的已导出配置，减少误用风险。

## 12. 推荐优先级

如果只做一轮最划算的优化，优先做以下 4 项：

1. 把 `public_endpoint`、`listen_port`、`public_port` 的说明写清楚。
2. 把 `local_networks` 定位为“家庭内网访问”的主配置项，弱化 `push_routes` 的普通用户曝光度。
3. 增加稳定的配置结果摘要。
4. 在文档和说明中把 `/share/openvpn_server/clients/` 与 `/share/openvpn_server/requests/` 的用途直接写明。

## 13. 结论

当前配置界面的核心问题不是能力不够，而是“用户意图”和“底层参数”之间缺少一层翻译。

本方案建议先做低风险的信息架构和说明优化，不急于引入独立 Web UI。这样可以在不破坏现有运行逻辑的前提下，显著降低误配置成本，并为后续的客户端管理界面奠定结构基础。
