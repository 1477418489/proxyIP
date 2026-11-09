# cfPROXYIP 开发计划

状态：首版实现与本地验收完成。2026-11-09。计划审查见 `docs/PLAN_REVIEW.md`，验收结果及未执行项见 `docs/VALIDATION.md`。

## 目标与边界

本地 Windows / Linux VPS 运行的 PROXYIP 候选采集、质量检测和 Web 管理工具，针对 Cloudflare Workers / VLESS 项目使用的反代地址。首版用 Python 3.11+、FastAPI、SQLite、原生 HTML/CSS/JS，提供单进程服务及 Docker 部署。不引入账号体系、Redis、前端构建链或互联网网段扫描。

必须明确：本地 TLS 检测通过不等于 Workers 可用；Worker 探针通过只代表所测目标与所测时间、路径，不承诺所有网站、UDP、VLESS 客户端兼容或长期可用。未配置 Worker 时可完整使用本地检测，并明确展示验证层级。

## 调研依据

- [Cloudflare TCP sockets](https://developers.cloudflare.com/workers/runtime-apis/tcp-sockets/)：connect() 阻止连接 Cloudflare IP、私网、localhost，不能把优选入口 IP 自动视为 PROXYIP。
- [zizifn/edgetunnel](https://github.com/zizifn/edgetunnel/blob/main/src/worker-vless.js)：PROXYIP 是 TCP 转发/重试目的地；TLS ClientHello 中原目标 SNI 会保留。实际路径的探测才有意义。
- [cmliu/CF-Workers-CheckProxyIP](https://github.com/cmliu/CF-Workers-CheckProxyIP)：参考候选解析、TCP→TLS→HTTP 的验证步骤，不复制实现。README 称 MIT，但实际 LICENSE 为 GPL-3.0；其自实现 TLS 仅部分校验证书，responseTime 是 TCP 耗时，不宜直接作为本项目信任或评分依据。
- [xgonce/Cloudflare_IP](https://github.com/xgonce/Cloudflare_IP)：result.csv 的候选为 `IP` + `端口`，`cf-meta-ip` 是出口观测。当前约 1,129 行；无 LICENSE，提供用户主动拉取入口，不打包再分发数据。
- [byJoey/cfnew-ipdb](https://github.com/byJoey/cfnew-ipdb)：MIT；全量约 102,257 行，首版提供 HK 子集作为可选来源，限制导入数量；来源标签和上游延迟都不能代替本地测量。
- [workerd sockets.c++](https://github.com/cloudflare/workerd/blob/main/src/workerd/api/sockets.c%2B%2B)：类型存在 `expectedServerHostname`，但实现可拒绝该参数。因此 Worker 不依赖未支持的 SNI 覆盖，不实现或复制 TLS 协议栈。

来源是 2026-11-09 读取到的快照；代码/数据可能变化。后续 README 保留这些边界及出处。

## 检测设计

1. 本地模式：解析候选域名到公网地址，固定实际连接 IP；TCP 连接该地址，TLS 的 SNI、证书校验和 HTTP Host 使用固定检测目标。访问 `/cdn-cgi/trace`，要求 HTTP 200、`h` 与目标一致、有效的 `ip`、`colo`，不跟随重定向。
2. Worker 模式：后端经鉴权 WebSocket 连接自部署 Worker；Worker 只向请求指定的公网 IP:port 创建一个 TCP socket，不尝试原目标或其他 IP。后端以 Python `ssl.MemoryBIO` 完成端到端 TLS，保持系统证书验证。Worker 先发带候选、colo、TCP 耗时的 ready 消息，之后仅转发二进制数据。
3. 默认两个固定目标：`www.cloudflare.com`、`speed.cloudflare.com`；每个目标默认重复 3 次。记录单次结果与失败阶段、实际连接 IP、检测模式、Worker colo、TCP / TLS / TTFB / 总耗时；总体提供成功率、P50、最大值、延迟标准差和明确公式的 0–100 评分。每轮最多 10 个样本，不展示缺乏样本支撑的 P95。
4. 评分公式：`round(80*r + 20*r*max(0, 1 - P50_ms/3000), 1)`，r 为完整检测的成功率，P50 仅统计成功的端到端耗时；零成功为 0 分，未完成不评分。只在同一模式、目标组合和检测机器上比较。部分失败标为不稳定，全部失败标为失败；至少一次成功才有延迟统计。质量不是带宽：本版不提供没有实际大文件测量依据的“下载速度”。
5. 导出先取每候选、每模式的最新检测尝试，再判断完整采样、全部通过及 24 小时 TTL。启动任务即创建候选结果占位，取消/重启中断的未完成结果会使旧成功失效；已完整完成的候选保留有效结果。界面始终显示模式、采样数与最后检测时间；Worker 未配置时禁用该模式并解释配置位置。本地失败不阻止 Worker 检测。
6. 单次尝试使用覆盖 DNS/WS/TCP/TLS/HTTP 的整体 deadline；Python 用 h11 处理 HTTP 分帧，必须收到完整响应。正文最多 64 KiB，头部最多 16 KiB，拒绝重定向及截断响应。Worker ready 为 `{type:"ready", protocol:1, candidate:"IP:port", colo:"HKG", connect_ms:12}`，后端核对候选和协议。隧道限时 25 秒、累计双向 256 KiB、单帧 64 KiB，读写按 await 排队并限制待写队列。

## 功能与接口约定

- 候选导入：手工多行文本 / 文本文件内容、IPv4/IPv6/域名及端口，默认 443；域名 A/AAAA 解析后展开为固定 IP；去重并保留来源。错误逐条返回，非公网、非法端口、URL/凭据格式拒绝。支持通用 `ip:port` TXT 及上述 CSV 的 IP/端口字段。
- 远程来源：内置两条可选来源；自定义 HTTPS TXT/CSV URL；主动拉取，保存最近拉取时间/计数/错误。每次下载最多 2 MiB，导入最多 500 个地址，候选库最多 5,000 条；截断必须明确提示，禁止静默丢弃。
- 任务：开始、进度、取消、重启后的中断标记；同一时刻只运行一个任务，默认并发 8、范围 1–16，重复 1–5、超时 3–20 秒。SQLite 保存任务、候选、结果历史；UI 轮询进度。任务启动时快照候选，运行中删除返回 409；每个候选逐样本保存，所有异常/取消均写终态。仅支持一个 uvicorn worker，多进程不在本版范围。
- 页面：中文单页，概览统计、导入/采集、任务设置、状态过滤、候选与最新质量指标、失败详情、历史详情、TXT/CSV 导出。候选删除和任务取消有显式控件。无 CDN 字体、JS 或样例假结果。
- API：`GET /api/status`、`GET /api/candidates`、`POST /api/import`、`DELETE /api/candidates/{id}`；`GET/POST /api/sources`、`POST /api/sources/{id}/fetch`；`POST/GET /api/jobs`、`GET /api/jobs/{id}`、`POST /api/jobs/{id}/cancel`；`GET /api/candidates/{id}/history`、`GET /api/export?mode=local|worker&format=txt|csv`。请求/响应与错误码随实现写入 README，FastAPI 提供 OpenAPI。

## 资源和访问边界

- 默认仅监听 127.0.0.1；可选管理 token，绑定非回环地址时无 token 必须启动失败；token 用 Authorization Bearer，不放 URL，前端仅内存保存。无 token 模式校验 Host 仅 localhost/回环，所有写操作检查 Origin 与当前 Host 同源。
- URL 仅接受 HTTPS；DNS 解析结果全部验证，实际连接固定到已验证地址，拒绝私网、保留地址、IPv4 映射绕过，不跟随重定向；网络请求不隐式使用环境代理。数据源、探针、导入数量、响应大小、连接时间都有界限。
- 所有外部内容以 textContent 渲染；禁止跨域写操作，token 模式还须鉴权。数据库和密钥不进入仓库。
- Worker 必须配置独立 secret；只接收公网 IP、合法端口、鉴权 Upgrade；限字节、限时、释放读写锁和 socket；断开/取消不遗留连接。

## 实施顺序与分工

1. 独立代理 review 计划，发现误判、安全或范围问题先修订并记录。
2. 后端与数据模型：解析、采集、SQLite、任务和评分；主代理负责。
3. 探测模块与 Worker：交给代理独立实现、协议测试，主代理对接接口。
4. Web UI：交给代理实现静态页，依据固定 API 合约对接。
5. 主代理完成集成、README、配置示例、Dockerfile/Compose、端到端检查和缺陷修复；代理复核关键实现。

## 验收

- `pytest`：地址规范化及 DNS 私网拒绝、CSV 正确取列、下载/导入上限、评分/P50、任务并发/取消/失败持久化、Worker 未配置、鉴权、最新失败/取消不会回退旧成功、过期导出、数据库重启恢复。
- 探针真实本地 TLS 测试夹具：正确 SNI/证书/trace 能通过；错误证书、200 HTML、重定向、截断/超限响应、超时都失败；Worker 通道不允许直连回退。
- `node --test`：Worker 地址验证、鉴权、超时/资源回收和协议处理；无需 Cloudflare 凭据的离线验证。
- 运行服务，通过浏览器实际操作导入、任务进度、筛选、详情、导出；查看桌面/移动布局与控制台错误。
- 小批公开候选作真实本地探测，失败也如实呈现；不执行大批扫描。没有 Cloudflare 账号/部署授权时不部署；明确报告 Worker 线上验证尚需用户配置，离线/本地模拟不能声称 Cloudflare 实测。
- 交付代码、可运行命令、计划审查记录和测试结果；不把等待外部配置的在线验证描述为已通过。
