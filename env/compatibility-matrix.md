# 依赖兼容矩阵

> **状态：未锁定。** 下表是待测项目清单，不是已验证结论。
> 规则：先确定一组可工作的组合，再生成 `uv.lock` 与容器镜像；不要先装最新版再逐个修冲突。

## 现状（已采集）

| 项 | 值 | 来源 |
|---|---|---|
| GPU | NVIDIA RTX A5000，23028 MiB（ECC 开启） | `env/hardware-report.json` |
| 计算能力 | 8.6（Ampere），**无 FP8 Tensor Core** | 同上 |
| 宿主机驱动 | 610.88 | 同上 |
| OS | Windows 11 专业版 10.0.26200 | 同上 |
| 主机内存 | 100504358912 B（约 93.6 GiB） | 同上 |
| 磁盘 | Samsung SSD 990 PRO 4TB，BusType=NVMe | 同上 |
| WSL2 / Docker | 未安装或未加入 PATH（PATH 中无 `docker` 命令） | 同上 |

## 已定：正式 GPU 环境路线（2026-10-28）

| 决策 | 结论 | 证据 |
|---|---|---|
| 正式 GPU 环境 | **WSL2 `Ubuntu-24.04`（version 2）** | 分发列表确认为 Ubuntu-24.04 / VERSION 2 |
| GPU 透传 | 可用 | WSL 内 `nvidia-smi` 返回同一张 RTX A5000 / 610.88 / 23028 MiB；`/usr/lib/wsl/lib/libcuda.so.1` 存在 |
| Windows 主机职责 | IDE、Git、客户端、文档；**不参与正式验收** | 文档 §14 |
| 容器镜像路线 | 暂不启用（Windows 侧无 Docker Desktop；WSL 内只有 docker CLI，daemon 未验证） | `hardware-report.json` |
| 虚拟环境 | `~/venvs/liteinfer`（Python 3.12.3，uv 0.11.26），由 `deploy/scripts/bootstrap_wsl.sh` 创建 | 脚本已执行成功 |

### 本环境的三条硬限制（必须写入后续所有报告）

1. **内存 31 GiB**，低于文档要求的 64GB 口径。训练 / 扩散探针需按 31 GiB 设计 batch 与分辨率；若需提升要改 `%USERPROFILE%\.wslconfig`（宿主机全局改动，未执行）。
2. **无直通 NVMe**：`lsblk` 显示全部 Virtual Disk，WSL2 根为 `ext4.vhdx` 虚拟磁盘。WBS-4 的 Swap NVMe 设备校验在此环境**无法证明**，只能通过 `wsl --mount` 挂直通盘，或改到 Linux 主机交付。
3. **宿主机无 CUDA Toolkit**（无 `nvcc`）。自研 CUDA 扩展的编译必须在容器或 Linux 主机进行，WSL2 内如需编译要单独安装 CUDA Toolkit 并锁定版本。

## 待锁定版本（WBS-0.2）

| 组件 | 候选版本 | 用途 | 约束来源 | 实测结论 |
|---|---|---|---|---|
| Python | 待定（>=3.11） | 运行时 | `requires-python` | 未测 |
| PyTorch | 待定 | 训练与张量运算 | 需 CUDA Runtime 与驱动匹配 | 未测 |
| CUDA Runtime（容器内） | 待定 | WS / 训练 | 与宿主机驱动分开记录 | 未测 |
| SGLang | 待定 | LLM 推理底座 | 与训练依赖是否冲突 | 未测 |
| Triton | 待定 | 算子 | 版本与 PyTorch ABI | 未测 |
| Diffusers | 仅对照实验 | 非生产训练 | 独立环境，不进生产链路 | — |

### 候选组合（2026-10-29 从参考源码读出，**未实测，不得当作锁定结论**）

来源是 `reference/` 里所拉 commit 的 `pyproject.toml`，commit 记在 `reference/SOURCES.lock.json`。
机器可读真源：**`env/candidate-lock.json`**；`python deploy/scripts/verify_dependencies.py` 会读它并与实装版本对照
（候选不符**不算失败**，未实测的 pin 不是验收依据）。

| 组件 | 候选 pin | 出处 | 备注 |
|---|---|---|---|
| Python | `>=3.11` | sglang `f2d31889258d` | vllm 另要求 `<3.15`；本项目取 3.12（与 WSL2 `~/venvs/liteinfer` 现存 3.12.3 一致，待实测） |
| PyTorch | `==2.14.1` | sglang `f2d31889258d` | 与 vllm 的 `==2.14.0` **冲突**，故 vllm 只作参考不装 |
| transformers | `==5.19.0` | sglang `f2d31889258d` | |
| flashinfer | `flashinfer_python[cu13]==0.7.2` | sglang `f2d31889258d` | 注释要求与 Dockerfile 内 jit-cache 版本对齐 |
| cuda-python | `>=13.0` | sglang `f2d31889258d` | 与宿主驱动 610.88 的 CUDA 13 匹配 |
| nvidia-cutlass-dsl | `[cu13]==4.8.0` | sglang `f2d31889258d` | |
| torchaudio / torchvision | `==2.11.0` / 未钉 | sglang `f2d31889258d` | 需与 torch 2.14.1 的 ABI 一并验 |

**冲突与取舍（写在这里，避免后面重复讨论）**

1. sglang 与 vllm 钉的 torch 不同（2.14.1 vs 2.14.0）⇒ 二者不能同时作为依赖。
   本项目以 **sglang 为唯一推理后端**，vllm 仅用于对照调度/背压设计（只读参考）。
2. A5000 为 sm86，**无 FP8 Tensor Core**：sglang 的 FP8 相关路径不进本机验收范围。
3. 以上 pin 必须在 WSL2 内实装并跑通最小用例后，才允许写进 `env/runtime-versions.json`
   与 `uv.lock`；在此之前 `pyproject.toml` 的运行时依赖**保持为空**。

## 不得违反的口径

1. 正式 GPU 验收环境为 Linux 容器 / WSL2 + 锁定镜像；Windows 主机不参与正式验收。
2. A5000 无 FP8 Tensor Core，首轮只验证 BF16/FP16 及适配硬件的 INT8/INT4 路径。
3. 宿主机 NVIDIA 驱动与容器内 CUDA Runtime 必须分开记录。
4. 扩散训练探针未通过时，记录阻塞点，不得宣称扩散训练已受支持。
5. 最终锁定矩阵必须以所选发布版本、A5000 实测和依赖兼容性为依据，不得照抄其他机器的版本号。
