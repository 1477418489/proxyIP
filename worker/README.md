# Worker 探针部署与使用

本探针部署在你自己的 Cloudflare 账号中，为 cfPROXYIP 的“Workers 路径”检测提供经过鉴权的 WebSocket → TCP 通道。它连接请求中指定的候选 IP；TLS 握手、证书校验、HTTP 和 trace 内容校验由 Python 后端完成。

本地 Web 管理服务和这个 Worker 是两个组件，需要分别运行。探针不提供 VLESS 节点或订阅。

## 选择部署方式

| 方式 | 所需条件 | 操作入口 |
| --- | --- | --- |
| Deploy to Cloudflare 按钮 | Cloudflare 账号、GitHub/GitLab 账号、实际公开仓库 | [部署助手](deploy.html)，填写仓库地址后生成按钮 |
| 控制台粘贴 | Cloudflare 账号；无需 Git 仓库和 Node.js | 将 [dist/index.js](dist/index.js) 全部粘贴到 Worker 编辑器 |
| Wrangler 命令行 | Cloudflare 账号、Node.js 20+、npm | 在本目录执行下文命令 |

当前尚未提供已发布的公开仓库地址，因此这里没有可直接使用的仓库部署按钮。没有仓库时可以立即使用控制台方式；上传后再用部署助手生成真实按钮。

## 先准备探针密钥

下载项目后，用浏览器打开本目录的 [deploy.html](deploy.html)（可直接双击），点击“生成随机密钥”。页面使用 Web Crypto 生成 64 位十六进制随机值；同一值分别配置为 Cloudflare 的 `PROBE_TOKEN` 和后端的 `CFPROXY_WORKER_TOKEN`。

部署助手只在当前页面内存中保存密钥，不使用浏览器持久存储，刷新后清除。部署链接和按钮 Markdown 不包含密钥；页面不会自动上传仓库、登录 Cloudflare 或执行部署。“复制后端配置”会复制真实密钥，即使页面当前显示的是掩码。请自行保管密钥，不要提交到仓库。

也可在本机生成：

```sh
python -c "import secrets; print(secrets.token_hex(32))"
```

Worker 要求 `PROBE_TOKEN` 至少 32 字符，建议直接使用上面生成的随机值。它与 Web 管理服务的 `CFPROXY_TOKEN` 是两个独立密钥。

## 方式一：Deploy to Cloudflare 按钮

1. 将本目录的**内容**上传为一个独立的 GitHub 或 GitLab 公开仓库，让 `wrangler.toml`、`package.json`、`src/` 位于仓库根目录。同时保留 `package-lock.json`、`.dev.vars.example` 和 `.gitignore`；不要上传真实密钥、`.dev.vars` 或 `.env`。
2. 打开 `deploy.html`，在“公开仓库或子目录 URL”填写真实仓库网页地址。若上传的是整个 cfPROXYIP 项目，填写实际 `worker` 子目录的网页地址：GitHub 使用 `…/tree/main/worker`，GitLab 使用 `…/-/tree/main/worker`，分支名按实际仓库修改。
3. 点击生成的“Deploy to Cloudflare”。按 Cloudflare 页面登录并连接 Git 账号，选择 Worker 名称，将准备好的随机密钥填入 `PROBE_TOKEN`，完成部署。Cloudflare 的流程会在你的 Git 账号中创建仓库副本。
4. 保存部署得到的实际 HTTPS `workers.dev` 根地址，按下文接入后端。

没有填写有效仓库 URL 时，助手的部署按钮不可用；它只检查 URL 格式，不验证仓库是否存在或公开。展开“将部署按钮加入仓库 README”可复制基于真实地址生成的 Markdown。

[Cloudflare 部署按钮](https://developers.cloudflare.com/workers/platform/deploy-buttons/)支持公开 `github.com` 和 `gitlab.com` 仓库，不支持私有仓库或自托管 Git 服务。部署子目录时，该子目录会成为新仓库根目录，必须独立包含所需文件；本 `worker/` 目录满足这一布局。`.dev.vars.example` 声明所需 Secret，`package.json` 提供其说明及部署脚本。

## 方式二：控制台粘贴，无需 Git 仓库

1. 打开 [Cloudflare Workers & Pages](https://dash.cloudflare.com/?to=/:account/workers-and-pages)，选择 **Create application → Start with Hello World**（或当前界面的新建 Worker 入口），取一个独立的名称并创建。
2. 进入该 Worker 的 **Settings → Runtime**，将兼容日期设为 `2026-11-01`，保留或添加 `nodejs_compat`，与项目的 [wrangler.toml](wrangler.toml) 一致。该日期已经默认启用 Node 兼容，显式标志仍可保留。
3. 打开本地 [dist/index.js](dist/index.js)，复制**全部内容**。进入 Worker 的 **Edit code**，替换默认代码并点击 **Deploy**。
4. 进入 **Settings → Variables and Secrets → Add**，类型选择 **Secret**，名称填写 `PROBE_TOKEN`，值填写准备好的随机密钥，保存并部署。不要选择普通明文变量，也不要将密钥写进 JavaScript。
5. 在 **Settings → Domains & Routes** 或部署结果中复制该 Worker 的实际 HTTPS `workers.dev` 根地址，接入后端。

控制台入口名称可能调整；若当前界面没有在线编辑功能，使用下方 Wrangler 方式。

`dist/index.js` 是生成好的单文件，保留 `cloudflare:sockets` 和 `node:net` 两个平台原生导入，不含本地相对模块依赖。不要直接粘贴 `src/index.js`；它依赖同目录的 `probe.js`。`dist/index.js.map` 是调试映射文件，不需要粘贴。

修改源码后，应先运行 `npm run bundle` 重新生成单文件，再复制到控制台。此操作需要 Node.js，但不需要登录 Cloudflare 或设置 Secret。

## 方式三：Wrangler 命令行

安装 Node.js 20+ 和 npm。在执行命令前检查 `wrangler.toml` 的 `name`：默认是 `cfproxyip-probe`，若账号中已有同名 Worker，先改成自己的唯一名称，避免更新到已有服务。

在本目录执行（从整个项目根目录进入时先运行 `cd worker`）：

```sh
npm test
npm run bundle
npx --yes wrangler@4.84.0 login
npm run deploy
npx --yes wrangler@4.84.0 secret put PROBE_TOKEN
```

最后一条命令会交互式要求输入密钥，将前面生成的值粘贴进去。先部署再添加 Secret 是预期顺序：没有有效 Secret 时，`/probe` 返回 503，不接受探测连接。`wrangler secret put` 会创建并部署包含该 Secret 的新版本。记录部署输出中的实际 HTTPS `workers.dev` 地址。

Worker 没有第三方运行时依赖；测试使用 Node 内建测试工具。脚本固定 Wrangler `4.84.0`，首次运行相关 `npx` 命令需要下载 CLI。`npm run bundle` 执行 `deploy --dry-run --outdir dist`，只生成产物，不部署，也不要求登录或密钥；`npm run deploy` 才执行实际部署。

## 接入 cfPROXYIP 后端

将以下两项写入**主项目根目录**的 `.env`，替换示例地址和说明文字，保留原有其他配置：

```dotenv
CFPROXY_WORKER_URL=https://实际Worker名称.实际子域.workers.dev
CFPROXY_WORKER_TOKEN=与Cloudflare的PROBE_TOKEN完全相同的随机密钥
```

`CFPROXY_WORKER_URL` 必须是实际 HTTPS 根地址，不带 `/probe`、其他路径、查询参数、片段或用户名密码；根路径末尾的 `/` 可以省略。后端会自行构造探针路径。密钥通过 `Authorization: Bearer` 传递，不放进 URL。

部署助手的第 3 步也可生成这两项配置。填写 Worker 根地址后点击“复制后端配置”，将内容合并到 `.env`，不要覆盖其他设置。

保存后停止并重新启动 Python 服务；在已安装项目依赖的 Python 环境中运行：

```sh
python -m cfproxy
```

使用项目 Docker Compose 时，在项目根目录运行：

```sh
docker compose up -d --force-recreate
```

单独执行 `docker compose restart` 不会重新读取容器的 `.env` 环境变量。宿主机启动的 Python 服务同样优先使用已有环境变量；若修改 `.env` 未生效，检查是否还有旧环境变量或旧进程。

刷新 Web 管理页面后，“Workers 路径”应可选。`GET /api/status` 返回的 `worker_configured: true` 只表示后端已配置地址和密钥，**不表示 Worker 已部署成功或可连接**。该 API 在启用管理鉴权时使用 `CFPROXY_TOKEN`，不是探针密钥。

## 更新代码与轮换密钥

轮换密钥时先生成新的随机值，再在 Cloudflare 的 **Settings → Variables and Secrets** 更新 `PROBE_TOKEN` 并部署；也可在本目录运行 `npx --yes wrangler@4.84.0 secret put PROBE_TOKEN` 输入新值。随后将主项目 `.env` 中的 `CFPROXY_WORKER_TOKEN` 改为同一值，重启 Python 服务；Docker 使用 `docker compose up -d --force-recreate`。新 Secret 生效后，旧值请求返回 401，更新后用少量候选确认连接。

源码更新方式与部署方式对应：

- 已连接 Workers Builds 的 Git 仓库：将变更推送到 Cloudflare 配置的部署分支，等待构建与部署成功；应更新 Cloudflare 实际连接的仓库副本。
- Wrangler：在本目录运行 `npm test`，通过后执行 `npm run deploy`。
- 控制台：运行 `npm test` 和 `npm run bundle`，再将重新生成的 `dist/index.js` 全部复制到 **Edit code** 并部署。

## 实际检测与结果解释

1. 先选 1–3 个候选，切换到“Workers 路径”，每目标采样 1 次、并发 1，再开始检测。
2. 打开结果详情，检查逐次样本的 TLS / HTTP 成败、错误阶段、总耗时、Worker 机房，以及观测出口 IP / 地区。
3. 确认配置和路径正常后，再扩大候选数量、重复次数与并发。

Worker 机房 `worker_colo` 来自当前执行探针的位置；`colo` 来自检测目标响应，二者都是机房代码。候选 IP 的 GeoIP 归属地区是数据库推测；观测出口地区来自本次成功 trace 响应的 `loc`，两者不能互相替代。若要筛选日本出口，在页面选择“观测出口 → 日本 JP”，不要仅依据候选归属或来源名称。

本地检测通过不等于 Workers 路径通过。Worker 检测通过只验证当时、该 Worker 路径和本项目检测目标的 TLS / HTTP 可用性，不验证完整 VLESS 客户端、所有网站、UDP 或下载带宽。来源下载和地区库更新所用的 HTTP / Mixed 代理不影响 Worker 检测连接。

本项目没有代你在 Cloudflare 账号中部署或完成生产路径检测。离线测试、单文件构建和本地 `wrangler dev` 都不能代替部署后的实际采样。

## 常见故障

下表的 HTTP 状态码指 **Worker 探针响应**。普通浏览器打开地址不能完成 WebSocket 探测，也不能据此判断候选质量。

| 现象 | 含义与处理 |
| --- | --- |
| Worker 根地址 `/` 返回 404 | 正常；本探针只处理 `/probe`，根路径没有欢迎页面 |
| 访问其他路径返回 404 | 检查地址和路由；后端配置使用根地址，不手工追加 `/probe` |
| `/probe` 返回 503 | Worker 的 `PROBE_TOKEN` 缺失或少于 32 字符；检查 Secret 是否添加到当前部署 |
| `/probe` 返回 401 | 已有有效 Worker Secret，但请求未携带正确 Bearer token；核对后端密钥和当前 Worker 的 Secret |
| 已鉴权的普通 HTTP 请求返回 426 | 缺少 WebSocket Upgrade；用项目的 Workers 检测模式调用 |
| 返回 400 | 在鉴权及升级请求检查通过后，IP 或端口不合法；必须是单个公网 IP 字面量和合法端口，不接受域名、私网或端口 25 |
| 返回 405 | 方法不正确；探针使用 GET WebSocket 握手 |
| 返回 502 | WebSocket 升级前候选 TCP 建连失败或超时；查看候选地址、端口、网络及 Cloudflare 出站限制 |
| 返回 403、登录页或验证码页 | 探针代码本身不返回 403；检查 Cloudflare Access、WAF、挑战规则及域名路由是否在握手前拦截请求 |
| Worker 模式仍不可选 | 检查两项后端配置是否同时设置、是否重启了正确进程；Docker 需要重新创建容器 |
| Worker 模式可选但检测失败 | 配置完整不等于连接成功；按逐次样本错误排查 Worker 鉴权、候选 TCP、TLS、HTTP 或 trace 校验 |
| TLS 或 HTTP 校验失败 | 端口开放不足以证明可用；核对具体错误，尝试其他候选，不关闭证书校验 |

[Workers TCP Sockets 限制](https://developers.cloudflare.com/workers/runtime-apis/tcp-sockets/)仍然适用，包括禁止连接 Cloudflare 自有 IP 段、私网及 localhost 等地址，以及禁止端口 25。本项目也会检查公网地址和端口边界；候选来自公开列表并不保证符合平台规则或能够连接。

## 协议和资源限制

请求为 `GET /probe?ip=公网IP&port=端口`，必须同时携带 `Authorization: Bearer <PROBE_TOKEN>` 和 WebSocket Upgrade。Worker 只接受 IP 字面量，不重新解析域名；禁止私网、回环、多播、保留地址、IPv4 映射 IPv6 和带 zone 的 IPv6。端口范围为 1–65535，排除 25。

连接候选成功后，第一条文本消息的结构如下：

```json
{"type":"ready","protocol":1,"candidate":"8.8.8.8:443","colo":"HKG","connect_ms":12}
```

IPv6 候选格式为 `[IPv6]:port`。后续帧均为二进制 TLS 字节；Worker 不接收检测目标域名，不执行 HTTP CONNECT / SOCKS，也不回退到其他地址。Python 后端校验证书链、主机名和有效期，不依赖 Workers 的 `expectedServerHostname` 参数。离线环境缺少 Cloudflare colo 时返回 `ZZZ`，不代表实际机房。

每连接限时 25 秒，双向累计最多 256 KiB，单个传入帧和待写队列最多 64 KiB。TCP 写入串行等待；TCP → WebSocket 的发送由平台实现，累计流量限制为缓冲设定上界。客户端关闭、TCP 失败、超时和超限都会关闭 socket 并回收 reader/writer。该限额只适合小型 trace 检测，不支持下载测速；后端单次样本整体超时最多 20 秒。

`npm test` 使用注入的 socket / WebSocket 验证鉴权、地址边界、协议与资源回收。第三方公开 JSON 检测 API 与本协议不兼容，不能直接作为 `CFPROXY_WORKER_URL` 使用。

## 官方参考

- [Deploy to Cloudflare 按钮及子目录要求](https://developers.cloudflare.com/workers/platform/deploy-buttons/)
- [控制台创建 Worker](https://developers.cloudflare.com/workers/get-started/dashboard/)
- [控制台 Hello World 与 Edit code 操作入口](https://developers.cloudflare.com/workers/versions-and-deployments/gradual-deployments/#via-the-cloudflare-dashboard)
- [Secret 的控制台与 Wrangler 配置](https://developers.cloudflare.com/workers/configuration/secrets/)
- [Node.js 兼容性](https://developers.cloudflare.com/workers/runtime-apis/nodejs/)
- [TCP Sockets API 与限制](https://developers.cloudflare.com/workers/runtime-apis/tcp-sockets/)
