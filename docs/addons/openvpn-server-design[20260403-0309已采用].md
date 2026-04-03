# Home Assistant OpenVPN Server Add-on 设计方案[20260403-0309已采用]

## 1. 背景与目标

本项目目标是在 `Home Assistant OS` 环境中实现一个 `Home Assistant Add-on`，提供完整的 `OpenVPN` 服务端能力，面向家庭或小型办公网络远程接入场景。

目标能力包括：

- 基于 Home Assistant Add-on 规范运行。
- 一键启停 OpenVPN 服务端。
- 可视化配置服务端基础参数。
- 自动初始化 PKI、服务端证书和客户端证书。
- 支持客户端账户管理、证书吊销和配置导出。
- 支持日志查看、状态查看、连接列表查看。
- 支持常见网络能力配置：`NAT`、转发、子网推送、DNS 推送。
- 支持数据持久化，升级后保留配置与证书数据。

## 2. 非目标

首版不纳入以下内容：

- 不实现 Home Assistant 前端面板卡片或独立 Integration。
- 不做多租户权限体系。
- 不做企业级 CA 分层审批流。
- 不做复杂的公网穿透能力，默认由用户自行保证端口映射和公网可达性。

## 3. 部署前提

- 运行环境为 `Home Assistant OS`。
- 通过 Add-on Store 安装本 Add-on。
- 宿主网络允许 `OpenVPN` 监听端口暴露。
- 用户自行完成路由器端口映射。
- Add-on 具备必要权限以执行 `tun`、iptables/NAT 和网络配置。

## 4. 方案对比

### 方案 A：纯 Shell + OpenVPN + EasyRSA

特点：

- 直接使用 `OpenVPN` 与 `EasyRSA` 生成和管理证书。
- 容器内通过 shell 脚本完成初始化、增删客户端、生成 `.ovpn`。
- 配置变更通过模板渲染完成。

优点：

- 依赖少，结构简单，容易贴近 OpenVPN 原生行为。
- 调试成本低。
- 适合作为首版快速落地。

缺点：

- 业务逻辑分散在脚本中，后期扩展 UI/接口时维护成本偏高。
- 数据模型和校验能力弱。

### 方案 B：Python 控制层 + OpenVPN + EasyRSA

特点：

- 由 Python 负责配置校验、数据落盘、客户端管理、状态汇总。
- OpenVPN 仍作为底层 VPN 引擎。
- Shell 仅保留系统初始化与少量网络操作。

优点：

- 配置模型清晰，便于扩展。
- 更适合后续增加 API、状态查询、导出逻辑和自动测试。
- 错误处理、日志结构和边界控制更容易做扎实。

缺点：

- 首版开发量大于纯 Shell。
- 需要定义更稳定的项目结构和模块边界。

### 推荐方案：方案 B

原因：

- 你要求“全部能力”一次纳入首版，意味着后续会涉及配置校验、状态展示、证书管理、客户端配置渲染和异常恢复。
- 如果用纯 Shell 堆叠，首版能跑，但二次迭代很容易出现脚本碎片化和状态不一致问题。
- `Python 控制层 + OpenVPN` 的组合更适合做成可维护的 Home Assistant Add-on。

## 5. 总体架构

建议采用以下分层：

1. `Home Assistant Add-on 包装层`
- `config.yaml`
- `Dockerfile`
- `build.yaml`
- `translations/*.json`
- `icon.png` / `logo.png`

2. `运行控制层`
- 负责读取 Add-on options。
- 校验配置。
- 生成运行时配置文件。
- 执行初始化流程。
- 管理服务启动、停止、重载。

3. `OpenVPN 服务层`
- 负责实际 VPN 隧道与客户端接入。
- 使用 `server.conf` 模板渲染。

4. `PKI 管理层`
- 基于 `EasyRSA` 管理 CA、服务端证书、客户端证书、CRL。

5. `管理接口层`
- 提供本地命令接口，供 Add-on 内部调用。
- 后续可扩展为轻量 HTTP API，用于页面或外部管理。

6. `持久化层`
- 使用 `/data` 保存配置、PKI 材料、生成的客户端文件、状态缓存。

## 6. 推荐目录结构

```text
openvpn_server/
├── docs/
│   └── addons/
│       └── openvpn-server-design[20260403-0309已采用].md
├── addon/
│   ├── config.yaml
│   ├── build.yaml
│   ├── Dockerfile
│   ├── rootfs/
│   │   ├── etc/
│   │   │   ├── cont-init.d/
│   │   │   └── services.d/
│   │   └── usr/local/bin/
│   ├── translations/
│   │   ├── en.json
│   │   └── zh-Hans.json
│   ├── icon.png
│   └── logo.png
│   ├── src/
│   │   └── openvpn_addon/
│   │       ├── __init__.py
│   │       ├── main.py
│   │       ├── config.py
│   │       ├── validator.py
│   │       ├── renderer.py
│   │       ├── pki.py
│   │       ├── network.py
│   │       └── status.py
│   └── templates/
│       ├── server.conf.tpl
│       └── client.ovpn.tpl
├── tests/
│   ├── test_config.py
│   ├── test_renderer.py
│   └── test_validator.py
└── README.md
```

## 7. Add-on 配置模型

建议对外暴露以下配置项：

### 基础配置

- `server_host`
- `listen_port`
- `public_port`
- `protocol`：`udp` / `tcp`
- `vpn_subnet`
- `vpn_netmask`
- `dns_servers`
- `push_routes`
- `device`：默认 `tun`

### 安全配置

- `cipher`
- `auth`
- `tls_crypt`
- `client_to_client`
- `duplicate_cn`
- `inactive_timeout`
- `keepalive`

### 网络配置

- `nat_enabled`
- `redirect_gateway`
- `local_networks`
- `public_endpoint`

说明：

- `listen_port` 是 OpenVPN 服务端在宿主上实际监听的端口。
- `public_port` 是写入客户端 `.ovpn` 配置中的端口，适用于公网映射端口与本地监听端口不一致的场景。
- `local_networks` 用于把家庭内网网段显式推送给 VPN 客户端，例如 `192.168.1.0/24`。
- 在 `Home Assistant OS` 上，客户端想访问内网时，不能只依赖 NAT；还必须把目标内网网段推送给客户端。

### 客户端管理配置

- `default_client_name`
- `client_expire_days`
- `client_max_count`

### 日志配置

- `log_level`
- `status_interval`

## 8. Add-on 需要的能力与权限

建议在 Add-on 中声明：

- `host_network: true`
- `devices: ["/dev/net/tun"]`
- `privileged`
- 必要的 `ports`
- `map: ["ssl", "share", "addons_config", "backup"]` 中按实际需要选择

说明：

- 是否需要 `full_access` 应严格评估，优先选择最小权限。
- `iptables` / NAT 是否可在当前基础镜像内执行，需要结合 HA Add-on 的实际运行能力验证。

## 9. 数据持久化设计

统一持久化到 `/data/openvpn`：

- `/data/openvpn/config/`
- `/data/openvpn/pki/`
- `/data/openvpn/clients/`
- `/data/openvpn/exports/`
- `/data/openvpn/runtime/`
- `/data/openvpn/logs/`

同时为了便于用户获取客户端配置，导出的 `.ovpn` 文件同步发布到 Home Assistant 共享目录：

- `/share/openvpn_server/clients/`

数据原则：

- 所有证书、私钥、CRL 必须持久化。
- 服务启动前检测目录完整性，缺失时走初始化或修复流程。
- 导出的客户端配置应可重复生成，不依赖临时文件。

## 10. 核心流程设计

### 10.1 首次安装初始化

1. 读取 Add-on options。
2. 校验配置合法性。
3. 初始化目录结构。
4. 若不存在 CA，则创建 CA。
5. 创建服务端证书、DH 参数或等效配置材料。
6. 生成 `ta.key` 或 `tls-crypt` 材料。
7. 渲染 `server.conf`。
8. 配置 NAT 和转发。
9. 启动 OpenVPN。

补充说明：

- 若宿主 `net.ipv4.ip_forward` 已开启，Add-on 内部无法写入 `sysctl` 不应视为致命错误。
- Add-on 启动时需要记录 NAT 规则是“新增”还是“已存在”，便于排查。

### 10.2 客户端创建流程

1. 接收客户端名。
2. 校验名称合法性与重复性。
3. 生成客户端证书。
4. 组装客户端 `.ovpn` 文件。
5. 写入导出目录。
6. 自动同步发布到 `/share/openvpn_server/clients/`。
7. 返回内部导出路径与共享目录发布路径。

明确操作入口：

- `openvpn-addon client create <name>`
- `openvpn-addon client issue <name>`
- `/share/openvpn_server/requests/<name>.json`

请求文件格式：

```json
{
  "action": "issue",
  "client_name": "iphone"
}
```

日志要求：

- 输出签发的客户端名。
- 输出 `/data/openvpn/exports/*.ovpn` 内部路径。
- 输出 `/share/openvpn_server/clients/*.ovpn` 共享发布路径。
- 输出 `/share/openvpn_server/results/*.result.json` 结果文件路径。
- 输出 `/share/openvpn_server/archive/*.json` 请求归档路径。

### 10.3 客户端吊销流程

1. 校验客户端是否存在。
2. 执行吊销。
3. 重新生成 `CRL`。
4. 更新服务端引用文件。
5. 可选执行热重载。

### 10.4 配置更新流程

1. 读取新配置。
2. 差异比较。
3. 判断是否需要重新签发证书或只重载服务。
4. 重新渲染配置。
5. 校验后应用。

## 11. 管理能力设计

建议首版至少提供以下本地管理命令：

- `openvpn-addon init`
- `openvpn-addon start`
- `openvpn-addon stop`
- `openvpn-addon reload`
- `openvpn-addon status`
- `openvpn-addon client create <name>`
- `openvpn-addon client revoke <name>`
- `openvpn-addon client list`
- `openvpn-addon client export <name>`
- `openvpn-addon validate`

后续可选扩展：

- 本地 HTTP 管理接口
- 简单 Web UI

说明：

- Home Assistant Add-on 本身不天然等于可视化管理后台。
- 如果你坚持“全部”包含 Web 管理界面，建议把“本地命令接口”作为第一阶段，“内置 Web UI”作为第二阶段。

## 12. UI 与交互建议

建议拆成两个层次：

### 第一层：Add-on 配置页

用于配置：

- 监听地址与端口
- VPN 网段
- DNS 与路由推送
- NAT 开关
- 日志等级

### 第二层：内置管理页面

用于管理：

- 客户端创建
- 客户端吊销
- 客户端配置下载
- 在线连接查看
- 服务状态和日志查看

结论：

- 如果只依赖 Add-on 原生 options，无法优雅覆盖“全部管理能力”。
- 因此建议内置一个轻量管理页面或 API。

## 13. 安全设计

- 默认关闭 `duplicate_cn`。
- 客户端名称做严格字符校验。
- 导出的 `.ovpn` 默认内嵌所需证书链，降低使用门槛。
- 私钥文件只保存在受控目录。
- 日志脱敏，避免暴露完整私钥内容。
- 对高风险操作如吊销、重置 PKI 增加二次确认。
- 默认使用相对现代的加密套件，避免旧算法默认暴露。

## 14. 异常与边界条件

- 首次初始化中途失败后的幂等重试。
- 端口已占用。
- `/dev/net/tun` 不可用。
- 宿主不允许所需网络权限。
- NAT 规则添加失败。
- 客户端重名。
- 证书已吊销但导出请求再次发生。
- 公网地址变化导致旧客户端配置失效。
- 升级后模板版本变化导致旧配置不兼容。

## 15. 测试策略

### 单元测试

- 配置校验。
- 配置模板渲染。
- 客户端命名规则。
- 导出内容拼装。

### 集成测试

- 容器初始化流程。
- PKI 初始化流程。
- 客户端创建与吊销流程。
- 服务配置渲染与启动验证。

### 手工验证

- Home Assistant OS 安装 Add-on。
- 创建客户端并导入手机/桌面端。
- 外网接入验证。
- NAT/路由访问内网验证。
- 吊销后重新连接失败验证。

针对内网访问的重点验证项：

- `local_networks` 至少包含家庭主网段，例如 `192.168.1.0/24`。
- 客户端连接后应获得到该网段的路由。
- Home Assistant OS 宿主 `ip_forward` 必须为 `1`。
- Add-on 日志应包含 NAT 规则应用结果。

## 16. 分阶段实施建议

### 阶段一：可运行基础版

- 完成 Add-on 基本骨架。
- OpenVPN 服务可启动。
- 支持基础 options。
- 完成 PKI 初始化。
- 支持单客户端生成。

### 阶段二：管理能力完善

- 客户端列表、导出、吊销。
- 状态查看与日志查看。
- 网络参数完善。

### 阶段三：可视化能力

- 增加轻量 Web UI 或 HTTP API。
- 在 Add-on 内完成客户端生命周期管理。

## 17. 主要风险

- Home Assistant Add-on 权限模型对 `iptables` 和 `tun` 的限制可能影响 NAT 与路由能力。
- 不同家庭网络拓扑下，端口映射、子网冲突和 DNS 推送问题会比容器内逻辑更容易成为上线阻塞点。
- 如果首版同时做内置 Web UI，交付周期和复杂度会明显提高。

## 18. 推荐落地结论

建议采用如下路径：

1. 先落地一个 `Python 控制层 + OpenVPN + EasyRSA` 的 Add-on。
2. 第一批交付聚焦“能安装、能初始化、能创建客户端、能连接、能吊销”。
3. 客户端证书策略采用“单人单证书”，首版默认内置一个基础客户端并允许继续增删。
4. UI 不建议与底层能力同时开工，先稳定配置模型和命令接口，再补轻量管理界面。

如果你认同该方案，下一步应将本文档状态更新为采用状态，并开始实现：

- `addon/` 基础骨架
- `src/` 控制层
- `templates/` 配置模板
- 最小可运行版本
