# 上传 GitHub 并部署 Worker

本项目可以整体上传一个仓库：仓库根目录用于 WSL/Docker Compose，`worker/` 是独立的 Cloudflare 探针项目。也可仅将 `worker/` 内容上传为单独仓库。

## 上传完整项目

发布目录只包含源码、测试、部署配置和文档。请使用Git上传，保留 `.gitignore`、`.dockerignore`、`.env.example`、`worker/.gitignore`、`worker/.dev.vars.example` 等隐藏文件。

1. 在GitHub创建空的公开仓库，例如 `cfproxyip`。暂时不要在GitHub初始化README，避免与本地文件冲突。Cloudflare官方部署按钮要求源仓库公开。
2. 在本地发布目录执行；如果已经存在 `.git`，跳过 `git init`：

```bash
git init -b main
git add .
git status --short
git commit -m "Prepare cfPROXYIP for Docker and Workers deployment"
git remote add origin https://github.com/你的用户名/你的仓库.git
git push -u origin main
```

上传前，`git status` 中不应出现真实 `.env`、`.dev.vars`、`data/`、`migration/`、SQLite、MMDB、`.venv/` 或缓存。已有远程仓库时先用 `git remote -v` 核对，不要盲目覆盖origin。登录GitHub请使用Git凭据管理器、SSH或GitHub提供的认证流程，不要把Token写进远程URL或提交到仓库。

未设置Git提交身份时，用自己的信息设置本仓库身份：

```bash
git config user.name "你的GitHub用户名"
git config user.email "你的GitHub提交邮箱"
```

## 部署 Cloudflare 探针

上传完成后，在浏览器打开本地 [worker/deploy.html](../worker/deploy.html)，填写真实地址：

- 完整项目：`https://github.com/用户名/仓库/tree/main/worker`。
- 独立探针仓库：`https://github.com/用户名/探针仓库`。

页面会生成官方“Deploy to Cloudflare”按钮。点击后按页面登录Cloudflare、连接GitHub、选择Worker名称并填写随机 `PROBE_TOKEN`。按钮链接不携带密钥。完整步骤见 [Worker部署指南](../worker/README.md)。

没有GitHub仓库时，也可以先用控制台部署：复制 [worker/dist/index.js](../worker/dist/index.js) 到Worker编辑器，设置兼容配置和 `PROBE_TOKEN` Secret。

## 接入现有 WSL 容器

在实际运行Compose的目录编辑 `.env`，保留原有端口、管理密钥、代理及其他设置，只补充：

```dotenv
CFPROXY_WORKER_URL=https://实际Worker名称.实际子域.workers.dev
CFPROXY_WORKER_TOKEN=与Worker的PROBE_TOKEN完全相同的随机密钥
```

运行 `docker compose up -d --force-recreate` 后，刷新管理页面，选择“Workers路径”，先检测1–3个候选。部署按钮成功或模式可选不代表候选可用，最终以实际TLS/HTTP检测结果为准。

若已经运行WSL版，更新应继续使用原Compose项目目录和数据卷。不要为代码整理随意改变目录名或 `COMPOSE_PROJECT_NAME`，否则可能创建新的空卷。部署与迁移细节见 [WSL指南](WSL_DOCKER.md)。
