# cfPROXYIP

在本地或 VPS 上采集、检测和管理 Cloudflare Workers / VLESS 项目使用的 PROXYIP。提供中文 Web 页面，数据保存在 SQLite，无需前端构建。

部署入口：[WSL / Docker Compose](docs/WSL_DOCKER.md) · [GitHub上传与Worker部署](docs/GITHUB.md) · [Worker详细使用指南](worker/README.md)。

**检测结果区分本地路径和 Workers 路径。** TCP 端口开放不算通过；必须以检测目标的 SNI / Host 完成严格 TLS 校验，收到完整 HTTP 200 响应，并验证 `/cdn-cgi/trace` 内容。

| 模式 | 实际路径 | 结果含义 |
| --- | --- | --- |
| 本地 | 本机 → 候选 IP → 检测目标 | 该机器的本地路径通过，Workers 尚未验证 |
| Worker | 本机 → 自部署 Worker → 指定候选 IP → 检测目标 | 该 Worker 路径通过，记录 Worker 机房和检测时间 |

两种模式独立运行。本地失败仍可进行 Worker 检测。本项目不生成 VLESS 节点或订阅，不验证完整 VLESS 客户端，也不把检测通过解释为支持所有网站、UDP 或长期可用。

## 本地启动

要求 Python 3.11 或更新版本。

`requirements.txt` 使用 `constraints.txt` 固定已验证的依赖版本；升级依赖时同步运行测试。

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m cfproxy
```

Linux / macOS：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -m cfproxy
```

打开 <http://127.0.0.1:8000>。默认没有候选数据，也不会自动拉取外部列表或发起检测。

1. 在页面粘贴候选地址、导入文本文件，或主动拉取一个预设来源。
2. 选择本地检测；需要验证 Workers 路径时，先配置下文的 Worker 探针。
3. 按需下载地区库，选择日本、美国等地区或全部地区；勾选候选，或按当前筛选与排序选择前 100 / 200 / 500 个。
4. 设置重复次数、并发数和超时并开始任务；查看成功率、延迟与失败阶段，再按当前模式和地区导出可用地址。

默认数据库为 `data/cfproxy.sqlite3`。备份时先停止服务再复制数据库文件；删除数据库会丢失候选、来源、任务和检测历史。

更新项目代码后，需要停止原 Python 服务，再在项目根目录重新运行 `python -m cfproxy`；Docker 使用 `docker compose up -d --build`。只刷新浏览器可能出现新页面配旧后端：例如地区库下载返回 `Not Found`，而 `/openapi.json` 没有 `/api/geo/update`。页面会提示“后端版本较旧，需要重启服务”并禁用下载；重启后刷新即可恢复，不要删除数据库。

## 输入与候选来源

手工输入支持每行一个 IPv4、IPv6 或域名，端口默认 443。IPv6 指定端口时使用 `[IPv6]:端口`。域名解析为当时的 A / AAAA 公网地址并保存为固定 IP，检测时不会再次把该域名解析到其他地址。

TXT 为每行一个地址，支持 `#` 注释。CSV 使用 `IP` 列，可选 `port` / `端口` 列，缺省端口为 443；预设 xgonce 来源使用 `IP` 与 `端口`，其中的 `cf-meta-ip` 是上游观测出口，不是连接候选地址。

| 预设来源 | 格式与用途 |
| --- | --- |
| [xgonce/Cloudflare_IP](https://github.com/xgonce/Cloudflare_IP) | [result.csv](https://raw.githubusercontent.com/xgonce/Cloudflare_IP/main/result.csv)，读取候选 IP 和端口 |
| [byJoey/cfnew-ipdb · 香港](https://github.com/byJoey/cfnew-ipdb) | [香港 TXT 子集](https://raw.githubusercontent.com/byJoey/cfnew-ipdb/main/country/HK.txt)，每行 `ip:port` |
| [FarelRA/proxyip-tracker](https://github.com/FarelRA/proxyip-tracker) | [tested-ips.csv](https://raw.githubusercontent.com/FarelRA/proxyip-tracker/main/result/tested-ips.csv)，使用 IP 列，缺省端口 443 |
| [Xiaobei09/proxyip](https://github.com/Xiaobei09/proxyip) | [新加坡 fast 分组](https://raw.githubusercontent.com/Xiaobei09/proxyip/main/data/valid/tiers/fast/SG.txt)，含注释的 TXT |
| [Wwuyi123/CF-Proxyip](https://github.com/Wwuyi123/CF-Proxyip) | [proxyip.txt](https://raw.githubusercontent.com/Wwuyi123/CF-Proxyip/main/proxyip.txt)，IPv4 TXT |
| [byJoey/cfnew-ipdb · 日本](https://github.com/byJoey/cfnew-ipdb) | [日本 TXT 子集](https://raw.githubusercontent.com/byJoey/cfnew-ipdb/main/country/JP.txt) |
| [byJoey/cfnew-ipdb · 新加坡](https://github.com/byJoey/cfnew-ipdb) | [新加坡 TXT 子集](https://raw.githubusercontent.com/byJoey/cfnew-ipdb/main/country/SG.txt) |
| [byJoey/cfnew-ipdb · 美国](https://github.com/byJoey/cfnew-ipdb) | [美国 TXT 子集](https://raw.githubusercontent.com/byJoey/cfnew-ipdb/main/country/US.txt) |

也可添加自己的 HTTPS TXT / CSV 来源，最多 50 个自定义来源，预设不占此额度。启动时按 URL 补充缺少的预设，保留已有来源的名称、格式、拉取记录，以及候选和历史；无需删除数据库。来源只在点击拉取时访问，本版没有定时采集或网段扫描。刷新来源会增加或去重候选，不自动删除以前导入的候选。

每次远程下载最多 2 MiB，每次导入读取最多 500 条原始记录，解析展开后保留最多 500 个去重候选，候选库最多 5,000 条。单域名最多展开 16 个地址；重复或非法记录也占原始读取额度。超过上限、非法行和解析错误会在导入结果中显示。来源 URL 不跟随重定向，拒绝私网、回环、保留地址及不安全的 DNS 解析结果。

TXT/CSV 来源下载要求 `Accept-Encoding: identity`，禁用自动解压；返回 gzip 等 HTTP 压缩内容的来源会被拒绝，避免压缩内容在大小检查之前占用过多内存。下文地区库使用的 `.mmdb.gz` 文件走独立的有界解压流程。

上游的延迟、速度、国家和 `verified` 标签不能代替本项目的检测。部分公开列表含 Cloudflare 自有 IP，而 [Workers 官方文档](https://developers.cloudflare.com/workers/runtime-apis/tcp-sockets/)明确限制 `connect()` 连接 Cloudflare IP 段；是否能用需要在实际 Worker 路径复测。

多个来源可能收录相同地址；Xiaobei09 本身是聚合项目，来源数量不代表底层数据完全独立。公开探针调查见 [PROBE_RESEARCH.md](docs/PROBE_RESEARCH.md)。

## 跨页选择候选

先设置国家/地区、搜索、状态筛选和排序，再输入数量 1–500，点击“选择前 N 个”，或使用 100 / 200 / 500 快捷选项。选择范围是全部匹配结果，不限当前页；每次操作替换已有选择。匹配数量少于 N 时，只选中实际存在的候选。

页面要求至少选中一个候选才可开始任务。空筛选结果、清空选择或逐项取消到零都会禁用开始按钮，不会意外检测全部地址。更改筛选后，可再次使用“选择前 N 个”替换之前的选择。

切换国家、地区依据或本地/Worker 模式会清空已选地址；地区库更新后，不再匹配当前地区的选中地址也会移除，避免跨地区混选。

## 按国家/地区查看与选择

国家筛选可选择日本 JP、美国 US 等地区，有两种依据：

| 地区依据 | 数据来源 | 可以说明什么 |
| --- | --- | --- |
| 候选 IP 归属地区，默认 | 离线 DB-IP Country Lite 数据库 | 数据库对候选 IP 的国家归属识别；这是地理推测，不等于物理位置或实际出口 |
| 观测出口地区 | 当前模式最新检测中，成功样本的 `/cdn-cgi/trace` `loc` | 所测时间、目标和路径下的来源国家识别；本地与 Worker 记录独立 |

`colo` 和 `worker_colo` 是机房代码，不作为国家字段，也不用于填补未知地区。来源名称中包含“日本”或“美国”同样不构成地区结论。

选择“观测出口：日本”表示至少一个成功样本返回 JP。若其他样本返回 US 或未知，页面会同时展示多地区和未知样本数，不表示全部采样都从日本出口。地区缺失不会让原本成功的 TLS/HTTP 样本失败，也不改变质量评分。

“未知”表示没有任何已知地区，不代表某个国家。候选归属未知可能是地区库尚未下载、库损坏或地址未被收录；观测出口未知包括未检测、旧记录未保存 `loc`，以及所有成功样本都没有有效地区。JP 加部分未知样本仍匹配 JP，未知样本数另外显示。旧检测不会用当前地理库回填历史出口，需重新检测才能获得新观测。

地区库不随项目打包。首次使用可点击页面的下载/更新按钮；启动和 GET 请求不会自动下载。使用 [DB-IP Country Lite](https://db-ip.com/db/download/ip-to-country-lite)，月度更新，支持 IPv4 / IPv6，无需账号或 API key。更新成功后，已有候选立即按新库展示，无需重导入；查询仍保留候选和检测历史。

更新可使用与来源拉取相同的 HTTP 代理开关和地址优先级。单次更新整体限时 60 秒，gzip 文件最多 8 MiB，解压最多 32 MiB；只有 UTC 当前月文件返回 404 时才尝试前一个月。下载和库校验成功后原子替换，失败保留旧库；页面显示更新状态、数据库版本和错误。

默认文件为 SQLite 所在目录下的 `dbip-country-lite.mmdb`，也可用 `CFPROXY_GEO_DB` 指定路径。只支持 DB-IP Country Lite MMDB；手工放入有效库后重启即可加载。没有有效库时，候选归属统一显示未分类，检测功能仍可使用。

IP Geolocation by [DB-IP](https://db-ip.com/)，数据按 [Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/) 授权。Lite 库覆盖率和准确性低于商业版本，地址分配与路由也会变化；页面提供来源署名，CSV 保留地理数据来源和版本。

## 通过 HTTP 代理拉取来源

远程来源默认直连。需要代理时，在来源区启用“使用代理”，填写本次代理地址，或留空使用 `.env` 的 `CFPROXY_FETCH_PROXY`。即使设置了环境默认地址，也要启用“使用代理”才会使用；未启用时直接连接来源。

支持 HTTP 代理和 Clash 等软件 Mixed 端口提供的 HTTP 协议。地址格式为 `http://主机:端口` 或 `http://用户名:密码@主机:端口`，例如 `http://127.0.0.1:7890`。端口必须显式填写，不接受路径、查询参数、片段、`https://` 或 `socks5://` 代理地址。HTTPS 来源通过 HTTP CONNECT 隧道访问。

代理地址的选择顺序为本次填写的非空地址、`CFPROXY_FETCH_PROXY`。两者都没有时返回明确错误；代理失败不会回退直连，也不会自动读取 `HTTP_PROXY` / `HTTPS_PROXY` 等通用环境变量。这套配置用于来源下载和主动地区库更新，本地检测与 Worker 检测仍使用各自原有的网络路径。

`127.0.0.1` 指运行 Python 后端的机器，不是打开浏览器的设备。允许显式指定本机或内网代理；来源本身仍须为公网 HTTPS 地址。来源 DNS 在后端解析，CONNECT 固定到校验后的 IP，同时保留原始 Host、TLS SNI 和证书校验。因此后端仍需能解析来源域名，代理不会接管这一步 DNS。

页面输入的代理地址只保存在当前页面内存与当次请求中，不写入数据库或浏览器存储。API 只返回是否配置了默认代理，不返回代理 URL 或凭据。使用带用户名、密码的代理时，VPS 的管理页面应通过 HTTPS 访问。

## 配置

启动时读取工作目录下的 `.env`，已有环境变量优先。配置变更后重启服务。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CFPROXY_HOST` | `127.0.0.1` | 监听地址；非回环地址必须设置管理 token |
| `CFPROXY_PORT` | `8000` | HTTP 监听端口 |
| `CFPROXY_DB` | `data/cfproxy.sqlite3` | SQLite 文件路径 |
| `CFPROXY_GEO_DB` | SQLite 同目录下的 `dbip-country-lite.mmdb` | 可选地区库文件路径；留空使用默认路径，只支持 DB-IP Country Lite |
| `CFPROXY_TOKEN` | 空 | 管理 API 的 Bearer token；设置时至少 16 字符 |
| `CFPROXY_FETCH_PROXY` | 空 | 来源下载/地区库更新的可选代理，例如 `http://127.0.0.1:7890`；仅在当次启用代理且未填写地址时使用 |
| `CFPROXY_WORKER_URL` | 空 | 自部署 Worker 的 HTTPS 根地址，例如 `https://your-probe.example.workers.dev` |
| `CFPROXY_WORKER_TOKEN` | 空 | Worker 的独立鉴权密钥，至少 32 字符，与 Worker 的 `PROBE_TOKEN` 相同 |

`CFPROXY_WORKER_URL` 和 `CFPROXY_WORKER_TOKEN` 必须同时设置。未配置时本地检测正常可用，Worker 模式不可选。

可用以下命令生成随机 token，分别为管理服务和 Worker 生成不同的值：

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

启用管理 token 后，在页面输入该值。页面只在内存保存 token，刷新后需要重新输入；API 通过 `Authorization: Bearer <token>` 鉴权。不要把 token 放进 URL 或提交到仓库。

## 部署 Worker 探针

本地管理服务与 Cloudflare Worker 探针需分别运行。Worker 提供经过鉴权的 WebSocket → TCP 通道，严格连接指定候选 IP；Python 后端通过通道完成 TLS 证书、HTTP 与 trace 校验。

完整步骤见 [Worker 部署与使用指南](worker/README.md)。下载项目后可直接双击 [部署助手 worker/deploy.html](worker/deploy.html)，生成随机密钥、真实仓库部署按钮和后端配置。

| 部署方式 | 入口 |
| --- | --- |
| Deploy to Cloudflare 按钮 | 将 `worker/` 内容上传为独立的 GitHub/GitLab 公开仓库，再在部署助手填写真实 URL；上传整个项目则填写实际 `worker` 子目录 URL |
| 无仓库，控制台粘贴 | 将 [worker/dist/index.js](worker/dist/index.js) 全部复制到 Worker 编辑器；兼容日期 `2026-11-01`，保留项目的 `nodejs_compat` 配置 |
| Wrangler 命令行 | Node.js 20+ 和 npm；按详细指南测试、构建、登录、部署，再设置 Secret |

目前没有已发布的仓库地址，所以没有现成可用的一键部署按钮；部署助手会在填写真实地址后生成。三种方式都需要在 Worker 中设置至少 32 字符的随机 **Secret** `PROBE_TOKEN`。然后将实际 HTTPS 根地址与同一密钥填入主项目 `.env`：

```dotenv
CFPROXY_WORKER_URL=https://实际Worker名称.实际子域.workers.dev
CFPROXY_WORKER_TOKEN=填写与PROBE_TOKEN相同的随机密钥
```

地址不带 `/probe`、其他路径、查询参数或凭据。探针密钥独立于管理服务的 `CFPROXY_TOKEN`。重启 Python 服务后选择“Workers 路径”；Docker 使用 `docker compose up -d --force-recreate` 重新读取配置。

先用少量候选实际检测，再查看逐次样本、Worker 机房与观测出口。模式可选或 `/api/status` 的 `worker_configured=true` 只表示配置完整，不代表线上可用；根地址返回 404 属于正常行为。离线测试和构建不构成 Cloudflare 生产路径验证，当前也没有代用户完成云端部署。探针的 25 秒、256 KiB 等限制及排错表见详细指南。

公开探针调研记录提供了第三方 API 地址与调用格式，本项目未默认接入。第三方 JSON API 与本项目的 WSS v1 通道不兼容，不能直接填入 `CFPROXY_WORKER_URL`。本轮 `pip.pack.dedyn.io/check` 对一个候选返回了 `success: true`，但未核实其证书校验和测量实现，只作第三方参考；其他入口出现运行环境错误或验证页。外部结果不会写成本地“Workers 路径通过”或参与导出。具体地址、响应及日期见 [公开探针调研](docs/PROBE_RESEARCH.md)。

## 质量指标与导出规则

固定检测目标为 `www.cloudflare.com` 和 `speed.cloudflare.com` 的 `/cdn-cgi/trace`。默认每个目标 3 次，合计 6 个样本；可设置每个目标重复 1–5 次。单次任务最多 500 个候选；任务默认并发 8，允许 1–16。每次样本整体超时 3–20 秒，覆盖连接、TLS 和 HTTP 读取；任务进度按完整完成的候选计数，详情显示逐次样本。

HTTP 正文最多 64 KiB、响应头最多 16 KiB。重定向、错误证书、HTTP 错误、截断响应，以及返回 HTML 但状态为 200 的响应都不能算通过。trace 中的 `h` 必须匹配目标域名，`ip` 和 `colo` 必须有效。

| 指标 | 口径 |
| --- | --- |
| 成功率 / 采样数 | 实际成功样本数与该轮已完成样本数；同时显示计划样本数 |
| TCP / TLS / TTFB / 总耗时 | 单样本的阶段耗时和端到端总耗时；Worker TCP 耗时由 Worker 记录 |
| P50 / 最大值 / 标准差 | 只统计该轮成功样本的总耗时；零成功时为空 |
| Worker colo | 执行 TCP 连接的 Worker 机房 |
| 出口 IP / 出口 colo | 检测目标返回的观测结果，与候选 IP、Worker 机房分别保存 |
| 观测出口地区 | 成功样本中有效的 `loc` 国家码；未知单独计数，不计入评分 |

每轮最多 10 个样本，首版不展示 P95，也不把延迟换算成下载速度。没有进行大文件传输，因此没有带宽结论。

只有完整完成的检测才评分，公式为：

```text
r = 成功样本数 / 计划样本数
score = round(80 * r + 20 * r * max(0, 1 - P50_ms / 3000), 1)
```

零成功为 0 分，未完成不评分。全部通过标为通过，部分成功标为不稳定，零成功标为失败。分数仅用于同一检测机器、同一模式、同一目标组合的比较；优先看成功率和原始错误。

导出先查每个候选在所选模式下的**最新检测尝试**，再要求完整采样、全部通过、未超过有效期，默认有效期 24 小时。它不会因为最新检测失败而回退到更早的成功记录。

国家/地区导出在以上条件成立后，再按当前国家和依据筛选。观测出口含某国即匹配，多国结果不会自动剔除；选择“未知”导出的是未分类结果，没有地理结论。CSV 在原有字段后增加 `candidate_country`、`exit_countries`、`geo_source` 和 `geo_version`，分别记录候选归属、观测出口集合、地理库来源与版本；TXT 仍为每行 `ip:port`。

启动新任务即为选中的候选创建结果占位；取消或进程中断后，未完成候选的旧成功不再用于该模式的导出。取消前已完整完成的候选保留结果。任务启动后新导入的候选不会加入当前任务，运行期间删除候选会被拒绝。

## Docker / VPS

在 WSL2 Ubuntu 中使用 Docker Compose，见 [WSL Docker 部署步骤](docs/WSL_DOCKER.md)，包含 Windows 源码复制、端口冲突、代理和现有数据迁移。

Docker 使用 Python 3.13 slim、非 root 用户和命名卷保存 SQLite。先复制 `.env.example` 为 `.env`，填写至少 16 字符的 `CFPROXY_TOKEN`，再启动：

```bash
docker compose up -d --build
docker compose logs -f
```

打开 <http://127.0.0.1:8000>。Compose 默认只把端口发布到宿主机回环地址，容器内监听 `0.0.0.0:8000`。如宿主机端口已占用，在 `.env` 设置 `CFPROXY_HTTP_PORT=8001`，然后访问 <http://127.0.0.1:8001>；此变量仅控制 Compose 宿主机端口，不改变容器内端口或直接运行 Python 时的 `CFPROXY_PORT`。未填写管理 token 时 Compose 会直接报错。

默认地区库位于 `/data/dbip-country-lite.mmdb`，与 SQLite 一起保存在命名卷，更新镜像不会丢失。若自定义 `CFPROXY_GEO_DB`，请使用容器内可写且已挂载的持久路径。

Compose 已通过 `env_file` 读取 `.env`，可直接配置 `CFPROXY_FETCH_PROXY`。容器内的 `127.0.0.1` 是容器自身；访问宿主机代理时，Docker Desktop 通常使用 `http://host.docker.internal:7890`。Linux Docker 如需同名入口，可自行在 `compose.yaml` 的 `services.cfproxy` 下增加：

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

代理必须监听容器可达的地址，也可直接填写可达的内网地址。本项目不会自动修改宿主机代理监听配置或 Docker 网络。

如果是在 WSL 中直接安装 Docker Engine，上面的 `host-gateway` 指向 WSL/Docker 宿主，不是 Windows。访问 Windows 代理请按 [WSL 部署说明](docs/WSL_DOCKER.md) 查询 Windows 网关地址。

```bash
docker compose down
```

停止并移除容器不会删除命名卷；`docker compose down -v` 会删除数据库。备份数据库前应先停止服务。

VPS 上建议保留回环端口，通过 Caddy / Nginx 等反向代理提供 HTTPS。若直接用 Python 监听非回环地址，也必须设置管理 token，并在上游提供 HTTPS。写操作检查浏览器 Origin 与 Host 同源；反向代理应保留外部 Host。页面静态文件公开，管理 API 仍须鉴权。

本版只支持**一个服务进程**。不要使用 `uvicorn --workers` 或多个容器共享同一个数据库，任务调度和取消状态由单进程管理。

## HTTP API

FastAPI 接口文档位于 `/docs`，结构定义位于 `/openapi.json`。启用 token 时，这两个路径与业务 API 都需要 Bearer header；页面中输入的 token 不会自动授权新标签页，直接打开 `/docs` 可能返回 401。命令行请求可自行携带 header。

| 方法与路径 | 用途 |
| --- | --- |
| `GET /api/status` | 探针配置状态、当前任务、候选数量和 `geo` 地区库状态；`fetch_proxy_configured` 只表示是否配置默认代理 |
| `GET /api/candidates?mode=local` | 当前模式下的候选与最新结果；支持 `search`、`status`、`country`、`country_basis` 筛选 |
| `POST /api/import` | 导入文本或 CSV，返回新增、重复、拒绝及截断信息 |
| `DELETE /api/candidates/{id}` | 删除候选；运行任务期间返回冲突 |
| `GET /api/candidates/{id}/history` | 历史汇总与原始样本 |
| `GET /api/sources` / `POST /api/sources` | 查看或添加来源 |
| `POST /api/sources/{id}/fetch` | 主动拉取来源；无正文为直连，可按次选择 HTTP 代理 |
| `POST /api/geo/update` | 主动下载更新地区库；接受相同的 `use_proxy` / `proxy_url` 请求字段 |
| `GET /api/jobs` / `POST /api/jobs` | 查看任务或创建检测任务 |
| `GET /api/jobs/{id}` | 任务进度与终态 |
| `POST /api/jobs/{id}/cancel` | 取消任务并关闭在途连接 |
| `GET /api/export?mode=local&format=txt&max_age_hours=24` | 导出当前模式下有效地址；支持 `txt` / `csv` 及 `country` / `country_basis` 参数 |

地区参数示例：

```text
/api/candidates?mode=local&country=JP&country_basis=candidate
/api/candidates?mode=worker&country=US&country_basis=exit
/api/export?mode=worker&format=csv&country=JP&country_basis=exit&max_age_hours=24
```

`country` 默认为 `all`，可传两位国家码或 `unknown`；`country_basis` 默认为 `candidate`，也可传 `exit`。候选响应增加 `country: {code, name, source, version}`，未知为 `null`；最新结果/历史汇总增加 `exit_countries` 和 `country_unknown_samples`，单样本增加 `exit_country`。`status.geo` 包含 `ready`、`version`、`build_date`、`source`、`error`、`updating`，不暴露库的本地路径。

导入 JSON 示例：

```json
{
  "text": "你的候选域名:443\n你的候选IP:8443",
  "source": "手动导入",
  "format": "auto"
}
```

通过代理拉取来源的 JSON 示例：

```json
{
  "use_proxy": true,
  "proxy_url": "http://127.0.0.1:7890"
}
```

`use_proxy` 默认 `false`。当它为 `true` 且 `proxy_url` 为空或省略时，使用 `CFPROXY_FETCH_PROXY`；当它为 `false` 时直连。无正文请求保持直连，兼容已有调用方式。成功响应在导入统计之外增加 `fetch_via: "proxy"` 或 `"direct"`，不包含代理地址。

创建任务的 JSON 示例；替换为实际候选 ID：

```json
{
  "candidate_ids": [1, 2],
  "mode": "local",
  "concurrency": 8,
  "samples": 3,
  "timeout": 10
}
```

为兼容已有 API 调用，`candidate_ids: []` 仍表示当前全部候选，且受单次 500 条上限约束。页面始终提交非空的显式选择，不使用这个兼容行为。

常见错误包括 400 导入/配置/网络错误、401 鉴权失败、403 Host 或来源检查失败、404 对象不存在、409 任务冲突、413 请求体超过 2 MiB、422 参数错误。自定义来源最多保存 50 条；历史 API 每候选返回最近 50 次记录，任务 API 返回最近 30 次任务。具体响应以 `/docs` 和实际返回的 `detail` 为准。

## 开发与验证

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
cd worker
npm test
```

测试覆盖地址与来源解析、资源限制、任务持久化、取消/重启、最新结果导出，以及真实本地 TLS 与模拟 WebSocket 隧道。运行这些测试不需要 Cloudflare 凭据；它们不代表线上候选可达。

源码测试可运行 `python -m pytest` 和 `cd worker && npm test`；WSL Docker 部署见 [WSL_DOCKER.md](docs/WSL_DOCKER.md)，Worker 探针部署见 [worker/README.md](worker/README.md)。

## 调研与出处

本项目独立实现检测代码，参考以下项目的协议行为和候选格式：

- [Cloudflare TCP sockets 文档](https://developers.cloudflare.com/workers/runtime-apis/tcp-sockets/)：运行时限制。
- [zizifn/edgetunnel](https://github.com/zizifn/edgetunnel/blob/main/src/worker-vless.js)：保留原 TLS ClientHello 的 PROXYIP 转发流程。
- [cmliu/CF-Workers-CheckProxyIP](https://github.com/cmliu/CF-Workers-CheckProxyIP)：分阶段检测思路。2026-11-09 调研时 README 写 MIT，但实际 LICENSE 为 GPL-3.0；本项目未复制其 TLS 代码。
- [workerd sockets 实现](https://github.com/cloudflare/workerd/blob/main/src/workerd/api/sockets.c%2B%2B)：`expectedServerHostname` 虽出现在类型定义，但运行时可能拒绝；本项目通过原始字节隧道与 Python TLS 避免依赖它。
- [xgonce/Cloudflare_IP](https://github.com/xgonce/Cloudflare_IP)：调研时未发现许可证，仅提供用户主动拉取链接，不打包上游数据。
- [byJoey/cfnew-ipdb](https://github.com/byJoey/cfnew-ipdb)：MIT，来源数据延迟由上游测得，不进入本项目评分。
- [DB-IP Country Lite](https://db-ip.com/db/download/ip-to-country-lite)：候选归属国家数据，CC BY 4.0；由用户主动下载，不随代码或镜像打包。

上游内容与候选可用性会变化；来源称“已验证”不等于此处的当前路径已经验证。
