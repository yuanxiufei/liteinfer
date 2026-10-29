# Runbooks

> 每个 runbook 必须是可执行命令序列，要求能在另一台机器上照着跑通。

待产出：

| 文件 | 用途 |
|---|---|
| `00-env-setup.md` | 从干净环境到体检通过 |
| `10-inference-smoke.md` | LLM / SDXL 独立推理冒烟与显存记录 |
| `20-memory-investigation.md` | 显存增长排查：NVML 与框架数据对照、泄漏定位 |
| `30-training-loop.md` | LoRA 训练、断点续训、合并验证步骤 |
| `40-swap-enable.md` | Swap 目录设备核验、启用前后对照、回滚 |
| `50-failure-drill.md` | 队列溢出、磁盘满、权重损坏、进程退出注入演练 |
| `60-release-check.md` | 发布门槛逐条核对（含锁文件、镜像、SBOM、文档） |

每个 runbook 结论要落到 `env/` 或 `benchmarks/` 的证据文件，不能只存在于脑子里。
