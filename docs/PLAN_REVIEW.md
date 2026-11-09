# 计划审查记录

2026-11-09，独立审查代理 research_projects 审查 PLAN.md；research_probe 交叉核查 TLS 方案。

结论：方案可行。实现前已采纳以下阻塞修订：

1. **慢流与响应误判**：逐 read 超时不足以限制总时长。每次尝试须整体 deadline；使用 h11 处理 HTTP Content-Length/chunked/EOF；完整响应、严格 TLS 和预期 trace 内容同时满足才成功。WS ready 协议、流量/帧/待写队列上限固定下来。
2. **过期成功复活**：先筛成功再取 latest 会掩盖后来的失败。任务开始时写占位结果，按候选+模式取最新尝试后再判断导出；未完成取消/中断使旧成功失效，完整完成的候选保留结果。
3. **任务生命周期**：启动时快照候选；运行中删除返回 409；每个样本保存原始结果，取消/异常/进程重启均写终态并关闭连接。首版仅单进程。
4. **统计样本不足**：默认每目标 3 次，不输出貌似稳定的 P95；显示实际样本数、P50、最大值、标准差。评分仅同模式/目标/检测位置可比较。
5. **检测路径独立**：本地失败不阻止 Worker 检测。后端严格 SSL MemoryBIO 经 Worker 原始 TCP 隧道；不使用虽然出现在类型里但运行时可能拒绝的 expectedServerHostname。

调查代码版本（后续上游可能变化）：

- workerd: `c995cb3671f69480f8f8a04afac8a1cb2fd9b763`
- cmliu/CF-Workers-CheckProxyIP: `a174663bef12dd071e103eacbeb9fe228ec79c00`
- xgonce/Cloudflare_IP: `b14d5a11151b8bbf5920fe8c3ab6a98623bda9d5`
- byJoey/cfnew-ipdb: `582531a12378ddd6da5a98e306855de3b0bc7667`

本项目独立实现代码。预设数据源只在用户主动拉取时访问，不打包其数据，不把上游速度/verified 作为本项目检测结论。
