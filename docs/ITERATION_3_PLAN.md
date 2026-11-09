# 第三轮：国家/地区展示与筛选

2026-11-09。状态：独立计划审查、实现审查和本地验收完成。

## 需求与数据含义

用户需要选择日本、美国等地区的 PROXYIP。候选池新增国家/地区列，支持按地区筛选、跨页选择和按地区导出。

区分两种信息：

- **候选 IP 归属地区**：离线 IP 地理数据库的识别结果，导入前后的所有公网 IPv4/IPv6 都可查，不等于物理位置或一定从该国出口。
- **观测出口地区**：成功 TLS/HTTP 检测的 `/cdn-cgi/trace` 中 `loc`，是目标对请求来源的国家识别。本地与 Workers 模式继续独立；`colo`/`worker_colo` 仍为机房代码，不转成国家填充。

地区不影响可用性或质量评分。缺少地区不把成功检测判失败；不从来源名称或上游标签推测国家。

## 调研依据

- [DB-IP Country Lite](https://db-ip.com/db/download/ip-to-country-lite)：月更、IPv4/IPv6、MMDB、CC BY 4.0；无需账号或 key。2026-11 版本实际 GET200，压缩约 4.2 MB、解压约 8 MB。显示结果的页面提供 [DB-IP](https://db-ip.com/) 署名链接。
- 官方下载 `https://download.db-ip.com/free/dbip-country-lite-2026-11.mmdb.gz`；下载链接根据当月生成，当前月404时仅回退前一个月，不依赖未知镜像。
- [maxminddb Python reader](https://github.com/maxmind/MaxMind-DB-Reader-python)：纯 Python 与可选扩展，支持 IPv4/IPv6。使用已有格式库读取，不自己解析 MMDB。
- [Cloudflare request.cf](https://developers.cloudflare.com/workers/runtime-apis/request/) 区分 country 与 colo；[trace 开源说明](https://github.com/fawazahmed0/cloudflare-trace-api) 表明 loc 是来源国家。实测 `2.7.113.69:443` 返回 `loc=FR`、`colo=MRS`，证明两字段不是同类信息。

## 实现

1. `cfproxy/geo.py` 管理离线库：默认位于 SQLite 同目录 `dbip-country-lite.mmdb`，可用 `CFPROXY_GEO_DB` 指定路径。启动加载已有有效库，无库/损坏时显示未知，不阻止检测。用内存 reader 避免 Windows 文件替换锁，并缓存 IP 查询；替换库后清空缓存。
2. `POST /api/geo/update` 下载当月官方库，复用来源拉取的 HTTP 代理开关/当次地址/环境默认优先级。单个更新锁、总 deadline 60秒、gzip下载上限8MiB、解压上限32MiB；写同目录临时文件，验证新内存reader、DBIP-Country-Lite类型/IPv6支持后 `os.replace`；仅替换成功后无await切换reader并清缓存。失败保留旧库、删除本次临时文件，不泄露代理凭据。更新状态包含在 `/api/status.geo`，页面可见进度/错误/版本，不在 GET 请求中自动下载。
3. 国家信息在 API 序列化候选时按 IP 查离线库，已有候选无需重导入或删除历史。返回 `candidate.country={code,name,source,version}` 或 null。中文名由数据库 names/前端 Intl.DisplayNames 提供，代码保持ISO两位码；未知不是某个真实国家。
4. probe 样本新增 `exit_country`，合法两位国家码保存；`XX`、`T1`、缺失/格式错误归为null。summary 新增 `exit_countries`、`country_unknown_samples`，所有成功样本合并，不静默丢弃冲突。旧历史读取时补空信息，必须重测才能获得当时loc，不能拿当前GeoIP回填历史观测。
5. UI 国家选择默认按“候选归属”，可切换“观测出口”；显示日本 JP、美国 US等动态列表及未知项。候选列同时展示归属与最新出口，多国显示完整集合/多地区。观测筛选含某国表示至少一个成功样本观察到该国，不能声称所有采样均为该国。历史展示每目标loc。
6. `GET /api/candidates` 与 `/api/export` 增加 `country=JP|US|unknown`、`country_basis=candidate|exit`，缺省all。CSV附加候选国家码和观测出口国家码，TXT保持ip:port。导出继续先最新、完整、全通过、TTL，再筛所选国家；UI发送所选国家/依据并注明导出范围。
7. 国家/依据/模式改变时清空已有选择，避免选择日本后仍提交之前美国的隐藏选择；批量前N遵守当前国家过滤。库更新造成选中地址不再匹配时也移除这些选择。
8. 增加 byJoey 美国地区预设，给用户现成的美国候选入口；来源标签仍不作为地区结论。

## 顺序与验收

先独立review，再并行实现：主代理地区库/下载/API/过滤，探针代理loc/汇总/测试，前端代理页面；同步README/.env/Docker路径说明。

测试覆盖下载/解压上限、当前月404回退、非法库保留旧库、更新锁/错误脱敏、IPv4/IPv6查询、无库/未知地区、旧记录读取、多目标地区冲突、latest/TTL与国家导出；浏览器验证日本/美国筛选、跨页选择、切换清空、CSV/TXT导出与移动布局。真实下载官方库并在已有1001个候选上补展示，保留数据。只做少量真实检测验证loc，不对整个候选池重测。

## 独立审查结论

docs_deploy 审查通过并固定：unknown 表示没有任何已知地区（出口有JP但部分未知仍匹配JP并显示未知样本数）；无库时所有候选归属未知，UI明确“未分类，不代表某个地区”。CSV加geo_source/geo_version。仅UTC当月404回退前月，失败/取消保留旧库；准备reader→原子replace→无await交换，避免内存/磁盘不同步。页面必须含DB-IP署名链接，二进制不入仓库。旧记录只默认空字段，不用GeoIP补历史loc。

实现审查补充：损坏 MMDB 的 reader 异常统一转为无效库，不阻止服务启动；gzip 捕获损坏/截断/解压上限；文件准备和校验后、替换前再次检查单调时钟截止时间。下载等待可被取消；有界的本地解压和磁盘写入同步执行，可能短暂占用事件循环。完整验收证据见 [VALIDATION.md](VALIDATION.md)。

## 后续修复：地区库下载返回 Not Found

2026-11-09实查8000服务为0.2.0，OpenAPI没有 `/api/geo/update`，状态没有geo；磁盘静态页面已是新版。代理与直连都在本机API层返回404，原因是旧后端未重启。

修复计划：先通过SQLite backup接口备份并核对无活动任务、进程身份和原数据库路径，再尝试在原8000端口加载新版；若自动审批拒绝则停止重启操作，不规避。前端在已取得status但缺少geo字段时禁用下载并显示重启说明，updateGeo同样提前返回；恢复新版status后自动恢复。同步README更新后需重启的说明，浏览器验证旧后端提示、新后端实际下载，以及候选/历史完整性。

research_probe独立审查通过：在线备份必须覆盖WAL，不能只复制主SQLite；保留原启动配置、端口与数据；初次加载或断网不能误判旧版本。此修复不修改下载代理行为或探针协议。
