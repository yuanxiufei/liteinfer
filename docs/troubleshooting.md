# 故障排查

> 随实施过程增补：每处理一次实验或运行故障，就补一条「症状 → 原因 → 命令 → 处理」。

## 体检入口

```bash
bash deploy/scripts/doctor.sh                  # Linux / WSL2
python deploy/scripts/verify_dependencies.py --json
python deploy/scripts/verify_gpu.py --json
```

## 常见场景（初版清单，WBS-8.9 文档交付时按实测补齐处理项）

| 症状 | 可能原因 | 检查命令 | 处理方向 |
|---|---|---|---|
| 服务启动后 GPU 未识别 | 容器未透传 GPU / WSL2 配置问题 | `nvidia-smi` | 核对 `deploy/docker-compose.yml` 的 GPU 配置与工具版本 |
| 依赖安装成功但导入失败 | ABI 不匹配、CUDA Runtime 与驱动不一致 | `python -c "import torch"` | 对齐 `env/compatibility-matrix.md`，不要直接升级到最新版 |
| 推理过程中显存持续增长 | 缓存或租约未释放、CUDA 异步未同步 | NVML 与框架数据对照 | 见 `runbooks/` 的显存调查流程（待补） |
| 从中间 checkpoint 恢复后 loss 不一致 | 优化器状态、随机数状态或步数未完整恢复 | checkpoint 关键字段对照 | 先复跑 `tests/regression` 的恢复对照 |
| 压力测试后主机内存不回落 | 队列无上限、取消后任务堆积 | 洪峰测试 | 检查背压与队列上限配置是否生效 |
| 加载第三方算子后服务崩溃 | 扩展未审核、未做进程隔离 | 查看进程退出日志 | 默认禁用未审核扩展，改用独立工作进程 + 配额限制 |
| Windows PowerShell 下 `python ... --json > out.json` 得到的文件无法解析（首字节 `0xFF`，中文乱码） | PowerShell 5.x 的 `>` 默认写 **UTF-16LE**，且管道会按控制台代码页重编码子进程输出 | `python deploy/scripts/verify_dependencies.py --json \| Out-File -Encoding utf8 out.json` | 需要落盘时用 `Out-File -Encoding utf8`，或直接读 stdout；WSL2 / Linux 下无此问题 |
| WSL2 下 `deploy/scripts/*.sh` 报 `/bin/bash^M: bad interpreter` | 文件被转成 CRLF | `file deploy/scripts/*.sh` | 仓库已加 `.gitattributes` 统一 LF；若仍出现，用 `git add --renormalize .` 重新规范化 |

## 安全边界提醒

- 服务进程不得直接加载未经审核的第三方 `.so`、`.dll` 或自定义 CUDA 二进制。
- 单纯进程隔离不足以抵御恶意本地原生代码，需要评估容器、操作系统安全策略与独立 GPU 设备边界。
- CUDA 设备或驱动故障可能影响同一 GPU 上的其他任务；独立 CUDA Context 不是硬件级安全隔离。
