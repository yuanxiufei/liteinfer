# ADR-0001: 正式 GPU 环境定为 WSL2 Ubuntu-24.04，Windows 主机不参与验收

- **状态：** accepted
- **日期：** 2026-10-29
- **关联 WBS：** WBS-0.1 / 0.2 / 0.3（基线 §14 运行环境策略）
- **决策者：** 项目 owner（单人主开发）

## 背景

基线要求「正式 GPU 验收环境为 Linux 容器 / WSL2 + 锁定镜像，Windows 主机只用于 IDE、Git、客户端与文档」。
本机实际条件（证据：`env/hardware-report.json`，2026-10-28 采集）：

- GPU：NVIDIA RTX A5000，23028 MiB（ECC 开启），计算能力 8.6（Ampere），**无 FP8 Tensor Core**；宿主机驱动 610.88。
- 主机：Windows 11 专业版 10.0.26200，内存约 93.6 GiB，系统盘 Samsung SSD 990 PRO 4TB（NVMe）。
- WSL2：`Ubuntu-24.04`（VERSION 2），GPU 透传可用（WSL 内 `nvidia-smi` 返回同一张 A5000；`/usr/lib/wsl/lib/libcuda.so.1` 存在）。
- Docker：Windows 侧未安装 Docker Desktop；WSL 内只有 docker CLI，daemon 可用性未验证。
- 虚拟环境：`~/venvs/liteinfer`（Python 3.12.3，uv 0.11.26），由 `deploy/scripts/bootstrap_wsl.sh` 创建。

需要现在定下来，否则 WBS-0.2 的依赖锁定没有目标环境，后续所有基准数据都无法复现。

## 决策

1. **正式 GPU 验收环境 = WSL2 `Ubuntu-24.04`（version 2）**，GPU 透传已实测可用。
2. **Windows 主机**只做 IDE、Git、客户端与文档；它跑出的任何数字都不作为验收证据。
3. **容器镜像路线暂不启用**：Docker daemon 未验证；待需要 CI 复现时再装 Docker Desktop 或改用独立 Linux 主机，并在本 ADR 的替代版本中登记。
4. 依赖先用 `uv` 装进 `~/venvs/liteinfer`，产出版本快照归档到 `env/`；`uv.lock` 在版本组合实测通过后再生成。
5. 所有基准报告必须标注 `simulated_constraint` 或 `physical_hardware`（基线 §22 口径）。

## 备选方案

| 方案 | 优点 | 缺点 | 未选原因 |
|---|---|---|---|
| Docker Desktop + nvidia-container-toolkit | 与 CI 环境一致，可锁镜像 digest | Windows 侧未安装，daemon 未验证；引入额外变量 | 先做能立刻验证的路线，容器路线作为后续独立 ADR |
| 双系统 Linux 主机 | 直通 NVMe、真实设备校验可行 | 需要额外机器与重装成本 | 仅在 WBS-4 的 NVMe 校验确实无法绕过时再立项 |
| 直接在 Windows 原生环境跑 | 无需额外环境 | 与本仓目标部署形态（Linux）不一致，Windows 不参与正式验收 | 明确违反基线 §14 |

## 影响

- 受影响模块：`env/`（硬件报告、兼容矩阵、版本台账）、`deploy/scripts/`（bootstrap / doctor / verify_*）。
- 需要的适配：训练与扩散探针按 **WSL2 可用内存 31 GiB**（低于基线 64GB 口径）设计 batch 与分辨率；如需提升必须改 `%USERPROFILE%\.wslconfig`（宿主机全局改动，未执行）。
- 验收方式：`bash deploy/scripts/doctor.sh` 在 WSL2 内通过；`python deploy/scripts/verify_gpu.py --json` 产出 GPU 快照并回填 `env/hardware-report.json`；LLM / SDXL 基线报告落在 `docs/benchmarks/`。

## 已知限制

1. **WSL2 可用内存 31 GiB** < 基线 64GB 口径：大模型与扩散训练的上限必须按此设计，报告中需显式记录。
2. **无直通 NVMe**：`lsblk` 显示全部 Virtual Disk，WSL2 根为 `ext4.vhdx`。WBS-4 的「Swap 目录必须是真实高速 NVMe」校验在此环境**无法证明**，只能 `wsl --mount` 挂直通盘或改到 Linux 主机交付。
3. **宿主机无 CUDA Toolkit（无 `nvcc`）**：自研 CUDA 扩展的编译必须在容器或 Linux 主机进行。
4. A5000 无 FP8 Tensor Core：首轮只验证 BF16/FP16 及适配硬件的 INT8/INT4 路径。
5. 当下列任一条件成立时本 ADR 需重审：Docker daemon 可用并决定切容器、换成独立 Linux 主机、或 WSL2 内存上限调整。
