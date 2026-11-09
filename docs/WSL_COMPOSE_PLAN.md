# WSL Compose 部署说明与配置计划

2026-11-09。需求：用户询问如何在WSL中用Docker Compose部署现有项目。

## 已查事实

本机WSL2发行版为Ubuntu 26.04，Docker Client/Server均29.6.1、Compose v5.3.0可用；使用WSL内原生Docker（unix socket、/var/lib/docker），并非Docker Desktop。Windows旧服务占用127.0.0.1:8000，8001在两边未监听。当前WSL NAT的Windows网关为172.26.128.1，地址可能随WSL重启变化。

项目已有Dockerfile、命名卷/data、容器内非root用户10001和强制管理Token；Compose当前宿主机端口写死8000。Windows现有1001个候选不在新Docker命名卷里，不能把启动新容器描述为自动迁移成功。

官方依据：[Microsoft WSL networking](https://learn.microsoft.com/en-us/windows/wsl/networking)说明NAT下从WSL访问Windows服务使用默认路由网关，目标服务须允许来自虚拟网络的连接；镜像模式的WSL localhost能力不等于Docker容器localhost。保留当前网络模式，不更改用户Docker服务、Windows代理或防火墙。

## 计划

1. Compose宿主机端口改为 `${CFPROXY_HTTP_PORT:-8000}`，默认行为不变；env示例和根README同步说明。用户本次部署设8001，容器仍监听8000，卷不变。
2. 写 `docs/WSL_DOCKER.md`，给从Windows目录同步源码到WSL家目录、创建.env/随机管理Token、可选Worker配置、Compose启动/校验/更新/日志的完整命令。同步源码时排除Windows虚拟环境、真实.env、运行库及工具缓存，不自动执行部署。
3. 专门解释原生WSL Docker访问Windows HTTP代理：动态查询Windows网关，使用实际HTTP/Mixed端口；允许虚拟网络连接且只按需设置，不把 `host-gateway` 错说成Windows地址。应用代理仅影响来源/地区库下载，不会替Docker镜像拉取或pip构建配置代理。
4. 可选迁移在首次启动前进行：Windows Python的SQLite backup输出一致快照，Docker命名卷导入时拒绝覆盖已有数据库，并给uid10001正确文件所有权；可复制已有MMDB。默认新建空池，文档明确选择。
5. 用现有WSL Docker验证Compose语法和端口插值（默认8000、自定义8001），校对脚本语法、路径和权限。用户询问部署方法，本轮不启动容器、不迁移用户库或调整系统配置，不把配置校验说成镜像/容器验收。

docs_deploy独立审查通过：保留回环发布，宿主机端口与容器端口分开；同步/初始化不得覆盖已有.env、数据或重新生成密钥；迁移仅允许空命名卷、uid/gid10001，不为获取卷先启动应用；Compose校验只输出必要结果，不打印真实Secret。

实现审查要求迁移复制、检查、构建、导入和启动使用一个 `set -e` 子shell，任一步失败就中止，不能在导入失败后继续启动空库；已修正，并要求新建迁移目录以避免复用残留快照。

已实现并在本机WSL校验：Compose默认端口8000、自定义8001均正确映射到容器8000，/data卷路径不变；8段bash命令和4段嵌入Python语法检查通过。没有构建镜像、启动容器或导入用户数据库。
