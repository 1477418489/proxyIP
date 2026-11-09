# 第二轮开发计划

2026-11-09。状态：实现与验收完成。对应用户提出的四项改进；测试与浏览器结果见 `VALIDATION.md` 的第二轮记录。

## 调研与实现范围

1. **跨页按数量选择**：候选池添加数量输入（1–500）、100/200/500 快捷选项、“选择前 N 个”和“清空选择”。按当前搜索、状态筛选、排序后的整个列表选择，替换已有选择，不限当前页；数量超出实际结果时选中全部匹配项并说明实际数量。空结果时不生成隐含的“全部候选”任务。
2. **现有 Worker 探针调查**：搜索 cmliu / ToiCF 等源码、README 明示的公开站点，仅少量请求验证可达性与结果，记录协议/鉴权/证书校验边界。公开 JSON API 与本项目 WSS v1 协议不混用，不把第三方 success 改写成系统严格 TLS 验证结果。将公开地址、实测结论及自部署选项写入文档；本轮未发现兼容 WSS v1 的公开探针，因此不默认接入。
3. **增加候选来源**：新增至少三个经过真实 GET 和格式核查的社区来源，另提供 byJoey 日本/新加坡地区分组；保留上游项目及数据时效说明。优先既有 TXT/CSV 格式，避免无必要解析器。启动时按 URL 去重添加，现有来源/候选/历史保留。所有上游 verified/速度均不进入检测评分。
4. **按次选择 HTTP 代理拉取**：用户已选择 HTTP/Mixed（例如 http://127.0.0.1:7890）。`POST /api/sources/{id}/fetch` 兼容无正文直连，新增可选 `{use_proxy:false,proxy_url:""}`。启用代理时优先本次地址，其次 `CFPROXY_FETCH_PROXY`；没有地址时明确报错。仅 HTTP 代理地址，支持 Basic 凭据。代理端允许用户显式填写本机/内网地址，127.0.0.1 指运行后端的机器。来源自身仍必须为 HTTPS 公网地址；先解析校验来源地址，CONNECT 固定到已校验 IP，保留原始 Host、TLS SNI 和系统证书校验，不跟随重定向。代理失败不回退直连。

## 接口与数据边界

- `/api/status` 增加 `fetch_proxy_configured` 布尔字段，绝不返回配置里的代理 URL/凭据。
- 本次填写的代理地址仅在页面内存/当前请求中使用，不写 localStorage、SQLite、日志或 API 返回。失败信息用明确且不含凭据的分类提示。
- 保留下载 2 MiB、整体超时、来源串行拉取和导入上限。代理只用于来源下载，不改变候选本地/Worker 检测路径与评分。
- `source-fetch` 请求体的代理字段用 SecretStr；另对其 RequestValidationError 返回固定说明，去掉 input/ctx/异常链和原始值，不能依赖 SecretStr 自动脱敏。网络失败统一分类，不使用包含 URL/凭据的原始异常字符串。文档给出 HTTP/Mixed 与 Docker 内 localhost 的含义。
- 代理请求使用 `URL(source).with_host(validated_ip)`，显式原始 `Host`（含非默认端口）及 `server_hostname`；代理自身用普通 resolver，来源用公网校验后的固定 IP。目标为 IPv6 时 CONNECT authority 保留方括号。总 deadline 包括来源 DNS。
- 页面始终要求显式选中至少一个候选再启动，覆盖空筛选/清空/逐项取消/刷新移除选择等路径；API 旧版 `candidate_ids=[]` 表示全部的行为为兼容保留，页面不再发送空集合。
- 新预设不占“50 个自定义来源”的额度；按 URL 保留已有自定义项的 ID/名称/格式/拉取记录，重复启动不重复插入。不能为添加预设而删除旧数据。
- 若公开探针无法可靠验证，记录实测失败/超时，不标记可用，不自动接入。

## 顺序与分工

1. 完成来源/探针调研并由独立代理审查此计划。
2. 前端代理负责跨页选择、代理选择控件、入口说明；主代理负责代理连接/API/默认来源及对应测试。
3. 同步 README、.env.example、调研与验收记录。
4. 定向自动测试和浏览器验证；保留现有用户数据库，重启当前服务加载改动。

## 验收

- 筛选排序后跨页选择 100/200/500；选取条数少于 N、空结果、清空、替换选择、最大限制行为正确。
- 在已有数据库再次启动，新增预设出现且不重复；所选预设真实 GET 成功且可解析（仅获取候选，不做大批检测）。
- 本地 HTTP CONNECT 代理集成测试：真实 HTTPS 来源经代理读取，校验 CONNECT IP、原始 Host/SNI、代理鉴权、下载上限；错误证书失败；拒绝私网来源；代理不可达时不回退直连；错误/API/持久化不泄露凭据。
- API 无正文保持直连；`use_proxy=true` 的本次地址/环境默认优先级正确，缺少代理明确报错。
- 公开探针调研写出具体 URL、结果、日期与协议兼容结论。
- 浏览器验证批量选择、拉取直连/代理成功和失败提示、移动端布局；最后跑必要的回归测试。

## 独立审查记录

docs_deploy 检查了 aiohttp 3.13.2 实现并复现 Pydantic SecretStr 校验错误仍可能包含 input。已采纳：固定 CONNECT IP + 原始 Host/SNI；代理显式允许回环/内网；fetch 接口 422 专门脱敏；空筛选结果防止意外检测全部。新增源使用 URL 唯一约束，保留现有数据。

来源调研选用：FarelRA/proxyip-tracker 的 result/tested-ips.csv（IP 列、默认443）；Xiaobei09/proxyip 的 data/valid/tiers/fast/SG.txt（注释TXT）；Wwuyi123/CF-Proxyip 的 proxyip.txt（IPv4 TXT）；byJoey 新加坡/日本地区TXT。前三个仓库当前源均实际 GET200，文件近期更新；Xiaobei09 是聚合项目，不能宣称与其它源底层完全独立。

公开探针初步实测：ToiCF 的 pr-apis.ekt.me/probe 与 api.090227.xyz/check 均HTTP200，但返回 `connect() is not available in Snippets`，不能用于判定候选；cmliu公开主页遇到验证页，API未获成功检测结果。公开接口与本项目原始WSS协议不兼容，暂不接入或标为可用。

后续找到 `pip.pack.dedyn.io/check` 单次返回成功，可作第三方参考；完整实测与协议边界见 `PROBE_RESEARCH.md`。实现复查补充两项修复：无端口 CSV 按 IP 表头自动识别；来源请求禁用自动解压并要求 identity，避免下载限制前发生大规模解压。
