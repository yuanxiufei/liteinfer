# 文档索引

- 开发基线（规划真源，WBS / 里程碑 / 验收标准）：[../LiteInfer 开发 WBS、里程碑排期与验收标准.md](../LiteInfer%20开发%20WBS、里程碑排期与验收标准.md)
- 进度看板：[wbs-status.md](wbs-status.md) — 里程碑与各阶段退出条件勾选，随执行更新
- 安装：[installation.md](installation.md) — 待 WBS-0 锁定依赖后填正式步骤
- 可复现构建：[reproducible-build.md](reproducible-build.md)
- 故障排查：[troubleshooting.md](troubleshooting.md)
- 参考源码：[../reference/README.md](../reference/README.md) — 上游参考项目快照（**不是依赖**），commit 锁定在 `reference/SOURCES.lock.json`
- 架构决策：[adr/](adr/) — 从 `0000-adr-template.md` 复制新建
  - [0001 正式 GPU 环境 = WSL2 Ubuntu-24.04](adr/0001-formal-gpu-environment-wsl2.md)
  - [0002 HTTP 传输层先用标准库](adr/0002-http-transport-stdlib.md)
- 架构说明：[architecture/](architecture/)
  - [overview.md](architecture/overview.md) — 分层、依赖方向与模块现状
  - [task-lifecycle.md](architecture/task-lifecycle.md) — 状态机、取消两阶段回执、回收记录
  - [adapters-and-weights.md](architecture/adapters-and-weights.md) — 适配器契约、注册中心、权重校验与释放证据
  - [scheduling-and-quota.md](architecture/scheduling-and-quota.md) — 出队顺序、静态配额与弹性借用、两类缓存与资源计量
  - [preemption.md](architecture/preemption.md) — 安全抢占：只在声明的安全点执行，默认不开放
- 运维脚本：[runbooks/](runbooks/) — 启动、验收、故障恢复流程
- 基线数据：[benchmarks/](benchmarks/) — LLM / SDXL / 训练 / Swap 实测报告

## 环境凭据（必须入库）

| 文件 | 内容 |
|---|---|
| `env/hardware-report.json` | GPU、驱动、主机内存、磁盘、OS、WSL2/Docker 状态 |
| `env/compatibility-matrix.md` | Python / PyTorch / CUDA Runtime / SGLang / Triton 版本组合与来源 |
| `env/runtime-versions.json` | 运行时与构建产物版本台账（含 CUDA 扩展编译参数与哈希） |
| `env/benchmark-baseline.json` | 固定负载下的性能与显存基线，供告警阈值比对 |

## 文档纪律

修改配置、接口或运行方式必须同步更新文档；每个里程碑关闭前生成 `acceptance-report.md`，
记录验收项、测试命令、结果、日志位置与遗留问题，不能只以代码已合并作为完成标准。
