# 公开 PROXYIP 探针调研

调研日期：2026-11-09。以下时间均为 UTC，北京时间加 8 小时。本轮仅使用同一个公开候选 `2.7.113.69:443`，每个列出的检测 API 请求一次；未批量测试，也未向这些服务发送本项目的管理或 Worker Token。

## 实测结果

| 服务与公开地址 | 实测时间 | 结果 | 本次能确认的事情 |
| --- | --- | --- | --- |
| `https://pip.pack.dedyn.io/check?proxyip=2.7.113.69%3A443` | 03:10 UTC | HTTP 200、JSON、`success: true` | 该公开检查 API 本次可调用，报告候选有效；可作第三方参考 |
| `https://api.090227.xyz/check?proxyip=2.7.113.69%3A443` | 03:07 UTC | HTTP 200、JSON、`success: false`，两个探测均报 `connect() is not available in Snippets` | API 可达，但当前执行环境不能建立探测所需的 TCP 连接；这是服务端基础设施错误，不是候选失败 |
| `https://pr-apis.ekt.me/probe?candidate=2.7.113.69%3A443` | 03:04 UTC | HTTP 200、JSON、`ok: false`，两个目标均报相同 Snippets 错误 | README 中公开的测试 API 可达，但当前无法完成 TCP 探测；同样不能据此否定候选 |
| `https://check.proxyip.cmliussss.net/`；`/check?proxyip=2.7.113.69%3A443` | 03:04–03:06 UTC | 首页返回 Cloudflare managed challenge 页面；检测路径请求发生 TLS EOF，未得到 JSON | 本次没有验证到可直接调用的检测 API，不能把网页可访问或仓库存在等同于 API 可用 |

`pip.pack.dedyn.io` 的关键响应原文：

```json
{
  "success": true,
  "proxyIP": "2.7.113.69",
  "portRemote": 443,
  "colo": "NRT",
  "responseTime": 484,
  "message": "第1次验证有效ProxyIP",
  "timestamp": "2026-11-09T03:10:26.774Z"
}
```

该响应的 `Server` 为 `cloudflare`，`CF-Ray` 以 `NRT` 结尾。响应头和服务自报的 `colo` 可以说明请求经过 Cloudflare，但不能单独证明后端一定在 Workers 中执行 `connect()`，也不能证明候选到目标的证书经过了完整校验。没有找到可与此部署对应的上游检测实现，`responseTime` 的具体测量范围也未核实，不将它与本项目的端到端耗时直接比较。

`api.090227.xyz` 返回 `colo: "LAX"`、`responseTime: 0`，IPv4/IPv6 子结果的 `connect_ms`、`tls_ms`、`http_ms` 都为 `null`。ToiCF 公共入口的两个目标同样没有连接耗时。这些空值和明确错误表明探测未实际进入候选 TCP 连接阶段；`success: false` / `ok: false` 在这里不代表 PROXYIP 质量差。

## 公开入口与源码依据

### pip.pack.dedyn.io

- [scuhot/proxyip-check-web README](https://github.com/scuhot/proxyip-check-web) 将 `pip.pack.dedyn.io/check` 列为可用性检测来源。
- [functions/check-proxy.mjs](https://github.com/scuhot/proxyip-check-web/blob/main/functions/check-proxy.mjs) 明确调用 `GET https://pip.pack.dedyn.io/check?proxyip=<ip:port>`，读取 `success`、`proxyIP`、`portRemote`、`colo`、`responseTime`、`message`。
- 本次请求不带鉴权也返回结果。该仓库本身是 Netlify 前端/转发函数，并未提供上述上游服务的 TCP/TLS 检测源码。因此不能由转发客户端推断上游运行时或证书验证规则。

### cmliu / api.090227.xyz

- [cmliu/CF-Workers-CheckProxyIP](https://github.com/cmliu/CF-Workers-CheckProxyIP) 的仓库首页地址是 `https://Check.ProxyIP.CMLiussss.net`。
- [wopupa21/Check-ProxyIP README](https://github.com/wopupa21/Check-ProxyIP) 将 `https://api.090227.xyz/check` 列为当前 JSON API，并说明公开入口可能变更。这是 API 地址的公开出处，不是该线上部署版本的证明。
- 核对的 cmliu 源码版本为 [`a174663bef12dd071e103eacbeb9fe228ec79c00`](https://github.com/cmliu/CF-Workers-CheckProxyIP/blob/a174663bef12dd071e103eacbeb9fe228ec79c00/_worker.js)。源码使用 `cloudflare:sockets`，提供 `GET /check?proxyip=...` 与 `POST /check` JSON 接口，无内置鉴权。
- 当前源码以 `ipv4.090227.xyz` / `ipv6.090227.xyz` 为 SNI/HTTP 目标，内嵌 TLS 客户端。它仅额外检查部分证书主机名匹配，明确不检查有效期、信任链；证书解析失败时放行。这个验证范围弱于本项目 Python/OpenSSL 的完整证书校验。
- 可部署源码与当前公开服务须分开判断：源码中存在 `connect()`，不代表本次公共入口的运行环境支持它。线上返回的 Snippets 错误应按实测处理。

### ToiCF

- [ToiCF/CF-Workers-CheckProxyIP README](https://github.com/ToiCF/CF-Workers-CheckProxyIP) 明确公布测试入口 `https://pr-apis.ekt.me/probe?candidate=...`。
- 核对源码版本为 [`23cca714303ab838a5d0630523c5e22cedf75b92`](https://github.com/ToiCF/CF-Workers-CheckProxyIP/blob/23cca714303ab838a5d0630523c5e22cedf75b92/Check_Proxy.js)。使用 `cloudflare:sockets`，接受 `candidate` 或 `candidates` 参数，也接受同名 POST JSON 字段，无内置鉴权。
- 源码检测目标是 `https://api.db-ip.com/v2/free/self` 和 `https://api.ip.sb/geoip`；返回 `ok`、`inferred_stack`、`probe_results` 及各目标的 TCP/TLS/HTTP 耗时。
- 内嵌 TLS 客户端的 `acceptCertificate()` 仅检查证书非空，未实现系统 CA 信任链、有效期与目标主机名的完整验证。因此即使公开服务返回成功，也不能直接等同于本项目的严格 TLS 验证通过。

另核查了 [ZedWAre-HK/ProxyIP-Check](https://github.com/ZedWAre-HK/ProxyIP-Check)：README 已声明不再维护，其 [Pages API 函数](https://github.com/ZedWAre-HK/ProxyIP-Check/blob/main/functions/api/index.js) 只是转发到 `code.run` 外部服务，不能作为在 Cloudflare Workers 内检测 TCP 的证据；本轮未再调用其候选检测。GitHub 仓库简介也可能滞后，例如 [yutian81/CF-Proxy-Checker](https://github.com/yutian81/CF-Proxy-Checker) 简介提 Workers，而当前 README 明确部署在 PHP 服务器，应以实际代码和部署说明为准。

## 与本项目的兼容性

这些公开服务提供的是 HTTP 请求→JSON 检测结果。它们均不实现本项目的协议：

```text
GET /probe?ip=<固定公网IP>&port=<端口>
Authorization: Bearer <独立密钥>
WebSocket Upgrade
→ ready {type, protocol:1, candidate, colo, connect_ms}
→ 双向二进制 TCP 字节
```

因此不能直接把这些地址填入 `CFPROXY_WORKER_URL`。特别是 ToiCF 同样叫 `/probe`，但参数和返回协议完全不同。本项目由 Python 在该字节通道上执行 TLS 握手与 HTTP 校验，第三方 JSON 无法替代这一步。

当前已确认的选择是：

- **第三方参考**：`pip.pack.dedyn.io/check` 本次可调用并返回成功，适合手工交叉参考。该结果只属于此服务、本次时间和该候选，未证明长期稳定性或严格 TLS 验证能力。
- **严格 Worker 检测**：部署仓库自带的 [worker/](../worker/README.md)，配置自己的 HTTPS 根地址和独立密钥，即可使用与本项目匹配的原始 TCP 通道。仍需实际部署后验证 Cloudflare 生产网络路径。
- **后续接入第三方 API**：应使用单独的第三方结果类型，记录服务地址、原始结果、检测时间和验证范围；将接口故障与候选检测失败区分。不能混写为本项目的“Workers 验证通过”，也不默认把候选名单提交到公开服务。

本次调研未修改默认配置，未接入第三方服务，未部署 Worker。
