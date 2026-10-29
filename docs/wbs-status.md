# 进度看板

任务定义与验收明细以开发基线为准：
[LiteInfer 开发 WBS、里程碑排期与验收标准.md](../LiteInfer%20开发%20WBS、里程碑排期与验收标准.md)
与 [V1.1 增补（RAM / VRAM / SSD 协同）](../LiteInfer%20开发%20WBS、里程碑排期与验收标准2.md)。
本文件只登记**状态、证据位置与遗留问题**，不重复任务描述；基线改动须评审，本文件随执行更新。

> 当前状态：**WBS-0 进行中（依赖未锁定），WBS-1 代码已落地但尚未按退出条件逐条验收。**
> 性能目标属建议验收门槛，不代表已有实测结果；骨架期没有产出任何 GPU 数字。

## 里程碑

| Milestone | 范围 | 状态 | 完成日期 | `acceptance-report.md` |
|---|---|---|---|---|
| `M0-foundation` | WBS-0 | 进行中 | — | — |
| `M1-core-sdk` | WBS-1、WBS-2 | 进行中（WBS-1 代码已落地，未验收） | — | — |
| `M2-inference-governance` | WBS-3 | 未开始 | — | — |
| `M3-training-lora` | WBS-4、WBS-5 | 未开始 | — | — |
| `M4-diffusion-lab` | WBS-6、WBS-7 | 未开始 | — | — |
| `M5-v1-release` | WBS-8 | 未开始 | — | — |

里程碑关闭前必须生成 `acceptance-report.md`，记录验收项、测试命令、结果、日志位置与遗留问题。
**不能只以代码已合并作为完成标准。**

## WBS 进度

| WBS | 周期 | 优先级 | 任务数 | 状态 | 备注 |
|---|---|---|---|---|---|
| WBS-0 技术验证与环境基线 | 第 1–2 周 | P0 | 7 | 进行中 | 0.1 硬件基线已采（`env/hardware-report.json`）；0.2 依赖未锁定，参考源码已拉、候选 pin 已成表、**待在 WSL2 实装验证**；0.3–0.7 未开始 |
| WBS-1 工程骨架与交付形态 | 第 3–4 周 | P0 | 8 | 实现完成（待验收） | 见下方「WBS-1 落地记录」 |
| WBS-2 模型适配器与权重管理 | 第 4–6 周 | P0 | 8 | 未开始 | 适配器协议待 WBS-0.2 锁定后端版本后落地 |
| WBS-3 推理调度、QoS、缓存与背压 | 第 5–8 周 | P0 | 10 | 未开始 | 队列上限 / 背压 / 取消已在 WBS-1 落地最小形态 |
| WBS-4 Swap、offload 与资源恢复 | 第 7–10 周 | P1 | 7 | 未开始 | 受限于 WSL2 无直通 NVMe（ADR-0001） |
| WBS-5 LLM LoRA 训练闭环 | 第 9–13 周 | P0 | 9 | 未开始 | |
| WBS-6 扩散训练与统一训练抽象层 | 第 14–16 周 | P1 | 8 | 未开始 | |
| WBS-7 算子管理、故障隔离与安全边界 | 第 10–16 周 | P1 | 6 | 未开始 | |
| WBS-8 回归、性能基线与稳定发行 | 第 17–20 周 | P0 | 9 | 未开始 | |
| WBS-9 RAM / VRAM / SSD 协同实验（V1.1 增补） | 基础链路稳定后 4–8 周 | P0/P1/P2 混合 | 10 | 未开始 | 实验轨道，不阻塞首个可交付版本；退出条件见 G6 |

## 参考源码（2026-10-29）

`reference/` 下放了上游参考项目的**浅克隆快照**，用于读实现与确认版本约束；
**不是依赖**（不写 `pyproject.toml`、不 `import`、内容不入库），纪律与清单见 [reference/README.md](../reference/README.md)。

| 项目 | commit | 用途 |
|---|---|---|
| sglang | `f2d31889258d` | 推理后端真源（WBS-0.2 / WBS-2） |
| vllm | `f5aa164b5148` | 调度与背压对照（WBS-3，只读） |
| peft | `89570547b9c2` | LoRA 训练参考（WBS-5） |
| diffusers | `40e551a4380c` | 扩散训练参考（WBS-6） |

复现：`python deploy/scripts/fetch_references.py --apply`（`--all` 含 optional 的 triton）。
上游已钉（**候选约束，未实测，不得直接当作锁定结论**）：sglang torch `==2.14.1`、
transformers `==5.19.0`、`flashinfer_python[cu13]==0.7.2`、Python `>=3.11`；vllm torch `==2.14.0`。
两个后端 torch pin 不同 ⇒ 本项目只把 sglang 当依赖，vllm 仅作对照。

**待执行（必须在 WSL2 内，Windows 主机不做）**：
`bash deploy/scripts/lock_dependencies.sh --dry-run` → 确认后实装 → 按脚本末尾步骤回填
`env/runtime-versions.json`、兼容矩阵，再 `uv lock`。脚本不代劳写入，避免未验证组合进锁定文件。

## WBS-1 落地记录（2026-10-29）

| 任务 | 落点 | 状态 | 证据 |
|---|---|---|---|
| 1.1 项目骨架 | `pyproject.toml`（ruff / mypy / pytest 标记）、`.github/workflows/ci.yml`、`.gitignore` | 完成 | CI 含 ruff、mypy、unit、integration 与依赖审计作业 |
| 1.2 核心领域模型 | `src/liteinfer/core/`（models / state / config / errors / ids） | 完成 | `tests/unit/test_state.py`、`test_config.py`、`test_errors.py` |
| 1.3 任务状态机 | `core/state.py` + `runtime/service.py` | 完成 | 迁移白名单、异常路径与恢复路径测试 |
| 1.4 SDK | `src/liteinfer/client.py`（`LiteInfer` / `session`） | 完成 | `tests/integration/test_sdk.py`（不依赖 HTTP） |
| 1.5 CLI | `src/liteinfer/cli/`（doctor / serve / model list / infer / task *） | 完成 | `tests/integration/test_cli.py` |
| 1.6 HTTP 服务 | `src/liteinfer/api/`（标准库实现，见 ADR-0002） | 完成 | `tests/integration/test_http_api.py`（真实起服务，非 mock） |
| 1.7 配置与日志 | `core/config.py`、`observability/` | 完成 | 请求 ID / 任务 ID 关联测试；未知配置项拒绝测试 |
| 1.8 CI 基础流水线 | `.github/workflows/ci.yml` | 完成（未实跑） | 尚未在任何 runner 上执行，GitHub Actions 未启用 |

### 本地实测（Windows 主机，非正式 GPU 环境）

| 命令 | 结果 |
|---|---|
| `python deploy/scripts/check_all.py` | 4 步全 OK（ruff / mypy / unit / integration） |
| `pytest -m unit -q` | 42 passed |
| `pytest -m integration -q` | 22 passed |
| `pytest -q`（全量） | 64 passed |
| `ruff check .` | All checks passed |
| `mypy`（strict） | Success: 27 source files |

**口径提醒**：上述结果在 Windows 主机 + CPython 3.14.5 上跑出，只证明纯逻辑链路，
**不构成 GPU 验收证据**；正式环境为 WSL2 Ubuntu-24.04（ADR-0001）。

### 遗留问题

1. WBS-0.2 未锁定：运行时依赖仍为空，因此 HTTP 传输层用标准库（ADR-0002），后续可能重审。
2. `tests/gpu/`、`tests/regression/`、`tests/chaos/` 仍为空：需要真实 GPU 与基线数据后才能写。
3. 默认执行器只有 `echo`：任何「推理可用」的结论都尚未成立。
4. 未做鉴权与租户安全边界：`tenant` 目前只用于排队配额统计。

## G0 退出条件

- [ ] 按文档可从干净环境完成安装。
- [ ] GPU 正确识别，目标 LLM 连续推理至少 30 分钟无异常。
- [ ] SDXL 独立推理成功，记录分辨率、步数、峰值显存和耗时。
- [ ] 至少一个训练样例完成前向、反向、优化器更新和 checkpoint 保存。
- [ ] 生成依赖锁文件和兼容矩阵，无未解释的关键版本冲突。
- [ ] 训练及扩散训练的已知限制明确记录。

未通过关键项时先排查，不进入资源治理开发。

## G1 退出条件

- [x] SDK 可在 Python 进程内直接提交并查询任务。（`tests/integration/test_sdk.py`）
- [x] CLI 和 HTTP 服务调用同一套核心领域服务。（CLI 走 HTTP 调 `TaskService`）
- [x] HTTP 服务停止不影响独立 SDK 的基本功能。（`test_sdk_still_works_after_http_service_is_stopped`：停服后确认连不上，且 SDK 仍能提交/等待/取结果）
- [x] 每个任务有唯一 ID、终态、错误信息及资源回收记录。（`record.to_dict()` 含 `reclaim`）
- [x] 核心模块单元测试 100% 通过。（42 passed）
- [ ] CI 能在每次合并请求中自动执行基础检查。（工作流已写，本地等价入口 `python deploy/scripts/check_all.py` 已跑通；
      **尚未在任何 runner 上实跑**，也无 PR 触发记录，故不勾）

## G2 前置验收条件（适配器）

- [ ] 至少一个 LLM 适配器和一个 SDXL 推理适配器可注册并独立调用。
- [ ] 非法配置、缺失依赖和不兼容版本能在任务启动前报出可读错误。
- [ ] 损坏权重不能被误判为有效模型。
- [ ] 适配器释放后，框架可观测资源回落；不能仅以 Python 对象删除作为回收成功依据。

## G2 退出条件

- [ ] 队列达到配置上限后拒绝新请求，内存不随请求总量持续增长。
- [ ] 压力测试结束后，待处理任务数量回落至正常范围。
- [ ] 两类缓存有独立预算、指标和淘汰日志。
- [ ] 固定负载下连续运行至少 60 分钟，无持续增长且无法解释的显存占用。
- [ ] 优先级、取消、失败和资源释放状态迁移测试全部通过。
- [ ] 只对已实现并通过恢复测试的任务类型开放安全抢占。

## Swap 验收要求

- [ ] 激活/梯度 Swap 目录通过设备检查，非合格 NVMe 时拒绝启用。
- [ ] 保存和恢复后，张量形状、dtype、内容及梯度语义正确。
- [ ] 磁盘满或校验失败时，任务安全失败，不继续使用损坏数据。
- [ ] 记录启用 Swap 前后的吞吐变化，决定是否默认启用。
- [ ] 同时报告峰值显存、step 时间、I/O 等待时间和训练收敛。

## G3 退出条件

- [ ] 固定数据集与配置可重复启动训练。
- [ ] 至少完成一次 LoRA 训练，loss 无 NaN/Inf，训练状态和日志完整。
- [ ] 从中间 checkpoint 恢复后继续训练成功。
- [ ] 固定测试输入下，独立加载 LoRA 与合并权重的输出满足预先设定的误差容差。
- [ ] 权重损坏、基座不匹配和配置不匹配均可检测。
- [ ] 训练产物能由 SDK 加载，并由独立推理流程验证。

## G4 退出条件

- [ ] 固定扩散训练样例完成前向、反向、优化器更新和 checkpoint 保存。
- [ ] 固定种子下，噪声、时间步和目标张量符合预期。
- [ ] 训练期间显存和主机内存可监控。
- [ ] 断点续训成功，关键状态均恢复。
- [ ] loss 曲线、生成样例及与参考实现的差异已归档。
- [ ] 若未通过数值与资源测试，标记为实验性功能并延长阶段，不宣称所有扩散模型已支持生产训练。

## G6 退出条件（V1.1 增补 · RAM / VRAM / SSD 协同）

- [ ] RAM、VRAM 和 SSD 使用独立指标与预算，未合并成一个总容量。
- [ ] CPU-only 至少一个模型可以完成固定长度推理，并记录速度与峰值 RAM。
- [ ] mmap / 分块加载与常规加载有可复现的对照数据。
- [ ] 至少一种静态 CPU/GPU 混合策略完成端到端测试（有 GPU 的环境）。
- [ ] 8GB / 24GB 约束测试报告明确标识模拟约束和真实硬件。
- [ ] OOM、内存不足、磁盘空间不足和读取失败都有可理解的错误及清理行为。
- [ ] 70B 结果按理论估算、加载成功、完成推理、达到性能目标分别报告，不得用其中一个替代其他结论。

## 发布门槛

- 所有 P0 测试通过。
- 不存在未解决的硬阈值失败。
- 关键软阈值退化均有解释、记录和批准。
- 依赖锁文件、容器镜像、模型清单和启动配置已归档。
- 所有明确不支持的能力均列入文档。
- 至少完成一次从干净环境部署到运行测试的完整演练。

## 风险追踪

全部沿用基线第 15 节 R-01 ~ R-10；已关闭或升级为阻塞项时在此登记。

| 风险 ID | 状态 | 触发/关闭依据 |
|---|---|---|
| R-01 SGLang 与训练依赖冲突 | 观察中 | WBS-0.2 未锁定；运行时依赖保持为空是主动选择，锁定前不引入新依赖 |
| R-02 24GB 显存无法满足混跑 | 未激活 | 待 LLM / SDXL 基线跑出后再评估 |
| R-11（本地增补）WSL2 可用内存 31 GiB < 64GB 口径 | 已激活 | `env/hardware-report.json`；训练 / 扩散探针需按 31 GiB 设计，或改 `.wslconfig`（宿主机全局改动，未执行） |
| R-12（本地增补）WSL2 无直通 NVMe，Swap 设备校验无法证明 | 已激活 | 同上；WBS-4 需 `wsl --mount` 挂直通盘或改到 Linux 主机交付 |
| R-13（本地增补）宿主机无 CUDA Toolkit（无 `nvcc`） | 已激活 | 同上；自研 CUDA 扩展编译必须在容器或 Linux 主机进行 |

## 自研 IR 立项条件（正式）

必须同时满足基线 13.1 的五项条件才启动：已有可重复生产负载与性能基线、已确认瓶颈来自 SGLang 特定组件、
替换模块有明确接口边界与验收集、可 A/B 测试、预期收益覆盖成本。**不得提前启动。**

## 已归档决策（ADR）

| ADR | 结论 | 状态 |
|---|---|---|
| [0001](adr/0001-formal-gpu-environment-wsl2.md) | 正式 GPU 环境 = WSL2 Ubuntu-24.04；Windows 主机不参与验收；容器路线暂不启用 | accepted |
| [0002](adr/0002-http-transport-stdlib.md) | HTTP 传输层先用标准库 `http.server`，依赖锁定前不引入 Web 框架 | accepted |
