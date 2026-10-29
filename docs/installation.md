# 安装

> 状态：**待 WBS-0 锁定依赖**。以下步骤为骨架说明，不是可承诺的安装方法。
> 版本数字必须来自 `env/compatibility-matrix.md` 的实测组合，不得写死"最新稳定版"。

## 目标环境

| 环境 | 用途 | 正式 GPU 验收 |
|---|---|---|
| Windows 主机 | IDE、Git、客户端、文档 | 否 |
| Linux 容器 / WSL2 | 本地开发与实验 | 是，但必须固定配置 |
| 锁定的 Linux 容器镜像 | CI、发布、复现验证 | 是 |
| 独立测试环境 | 验证部署脚本与兼容性 | 是 |

## 步骤（待填）

1. 安装 Python 解释器：`requires-python >= 3.11`，正式版本以兼容矩阵为准。
2. 安装 `uv` 并用 `uv sync --frozen` 依据 `uv.lock` 重建依赖。
3. 安装 PyTorch / CUDA Runtime：容器内版本单独记录，与宿主机 NVIDIA 驱动分开。
4. 安装 SGLang 固定版本，跑 `deploy/scripts/verify_dependencies.py` 校验。
5. 安装 LiteInfer 本体：`uv pip install -e .`（开发）或按 wheel 安装（发布）。
6. 执行体检：`python deploy/scripts/verify_gpu.py`、`bash deploy/scripts/doctor.sh`。
7. 记录结果到 `env/hardware-report.json` 与 `env/benchmark-baseline.json`。

## 已知硬件注意项

RTX A5000 为 24GB Ampere GPU，**没有 FP8 Tensor Core**。首轮性能验证以 BF16/FP16 及适配硬件的
INT8/INT4 路径为主，不把 FP8 作为硬件加速验收前提。

Swap 目录要求真实高速 NVMe；仅凭挂载路径名称包含 `nvme` 不能证明底层设备是 NVMe，启用前必须做设备检查。
