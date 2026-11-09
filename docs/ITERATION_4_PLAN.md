# 第四轮：Worker 部署与使用

2026-11-09。状态：调研、独立计划审查、实现审查和本地验收完成；尚未进行Cloudflare账号内实际部署。

## 需求和当前条件

用户需要完整的探针部署步骤、使用方法，并期望 Cloudflare 一键部署。用户确认尚未上传仓库；当前没有 Git 远程地址或本次提供的 Cloudflare 账号授权信息。先交付可发布模板、部署辅助入口和控制台部署成品，不编造可用的仓库按钮或线上部署结果。

## 官方依据

- [Deploy to Cloudflare buttons](https://developers.cloudflare.com/workers/platform/deploy-buttons/)：`https://deploy.workers.cloudflare.com/?url=<公开仓库URL>`；支持 GitHub/GitLab，子目录必须完全独立，部署时该目录成为新仓库根目录。
- 同一文档支持 `.dev.vars.example` / `.env.example` 声明 Worker Secret，`package.json.cloudflare.bindings` 提供说明；读取 `scripts.deploy` 作为部署命令。
- [Dashboard](https://developers.cloudflare.com/workers/get-started/dashboard/)：Workers & Pages → Create application，可导入/克隆 Git 仓库部署，提供 workers.dev 域名。
- [Secrets](https://developers.cloudflare.com/workers/configuration/secrets/) 与 [Node.js compatibility](https://developers.cloudflare.com/workers/runtime-apis/nodejs/)：密钥保存在 Worker Secret，`node:net` 需要 `nodejs_compat`；控制台部署需要保留配置。
- 官方 [saas-admin-template](https://github.com/cloudflare/templates/tree/main/saas-admin-template) 使用 `/tree/main/目录` 作为按钮来源；[worker-publisher-template](https://github.com/cloudflare/templates/tree/main/worker-publisher-template) 的 `.dev.vars.example` 使用空Secret值，并说明部署时填写。
- [Dashboard 渐进部署指引](https://developers.cloudflare.com/workers/versions-and-deployments/gradual-deployments/#via-the-cloudflare-dashboard) 仍给出 Hello World → Edit code 流程，作为无仓库手工部署的依据。

## 实现计划

1. 保持 `worker/` 完全独立；新增 `.dev.vars.example` 声明空的 `PROBE_TOKEN`，在 package.json 补部署字段说明。根目录及worker独立 `.gitignore` 排除本地真实密钥文件、保留空示例。Worker 中现有“未设密钥则503”行为保留。
2. 增加本地部署辅助页面 `worker/deploy.html`：生成随机探针密钥（Web Crypto），复制 `.env` 示例；用户填写公开 GitHub/GitLab 仓库或目录 URL 后生成实际 Cloudflare 部署按钮/Markdown。没有仓库地址时禁用仓库部署按钮，提供控制台步骤入口。URL使用标准编码，不包含任何密钥；不自动发布仓库或登录Cloudflare。
3. 使用固定版本 Wrangler 的官方打包（`deploy --dry-run --outdir dist`）生成 `worker/dist/index.js` 单文件；通过 `npm run bundle` 重建。它保留平台的 `cloudflare:sockets` / `node:net` 导入，控制台需要设置兼容日期及 `nodejs_compat`，不包含本地模块引用/用户密钥。
4. 重写 `worker/README.md`：推荐的一键部署步骤（上传独立目录或完整仓库worker子目录）、无仓库的控制台粘贴步骤、Wrangler命令行备选、密钥生成/设置/更新、后端.env/重启/Docker接入、Workers模式的少量验证、地区归属和观测出口区别、常见401/403/404/426/502/503与Cloudflare出站限制。根README精简入口并同步相关说明。
5. 验收：现有Worker测试，Wrangler实际dry-run产物检查（单文件、无密钥、无本地import），独立目录再打包，浏览器验证部署页面URL/按钮/复制操作与移动布局；校对官方依据与文档命令。未使用Cloudflare账户部署时明确标注未完成线上验证。

## 审查关注点

一键部署必须指向用户真实公开仓库；子目录隔离不能依赖Python后端或根配置；秘密变量留空且不进入按钮URL、浏览器存储或仓库；控制台单文件的兼容配置必须完整；dry-run和本地测试不能冒充生产部署成功。

## 独立审查结论

docs_deploy 审查通过，实施条件如下：部署页只接受无凭据/片段的 HTTPS GitHub/GitLab URL，使用URL编码与textContent，不在无仓库时放占位假按钮。worker目录独立忽略规则不依赖项目根。`cloudflare.bindings.PROBE_TOKEN.description`严格按官方格式，只做说明；Secret由部署页填写或部署后Dashboard设置。固定Wrangler由npx下载，dry-run要在独立目录实际验证，无账号/密钥要求。dist为可交付单文件成品，源码改变需重新构建；实际检查仅包含平台导入，无本地相对依赖。CLI提示name冲突处理。辅助页面无外部脚本和持久化，复制失败提供手动选择。

官方Node兼容文档说明2026-08-04起日期已默认启用Node兼容；本项目保留已有显式`nodejs_compat`以使仓库/控制台配置一致，官方允许保留。文档不把本地workerd或语法检查等同Cloudflare线上部署。

实现审查修正了辅助页Worker地址校验：拒绝IP、本地域名、0端口和反斜杠，避免生成后端不接受的配置。独立模板目录实际打包成功，产物与原目录逐字节一致。验收记录见 [VALIDATION.md](VALIDATION.md)。
