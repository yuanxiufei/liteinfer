# LiteInfer

通用训推基础设施（RTX A5000 单卡首发）— Python SDK / CLI / 独立 HTTP 服务 / 自动化测试 / 锁定环境 / 性能基线。

起步底座为 SGLang（仅用于 LLM 推理及其已验证的原生能力），训练基础设施与模型范式适配器由 LiteInfer 独立建设；
Diffusers 只作为可选对照验证工具，不进入生产训练执行链路。

## 项目状态

骨架已建立，**尚未执行任何 GPU 验收**。所有 WBS 任务均为计划项，性能目标为建议验收门槛，
不代表已有实测结果；骨架期没有产出任何 GPU 数字。

进度看板：[docs/wbs-status.md](docs/wbs-status.md)
开发基线：[../LiteInfer 开发 WBS、里程碑排期与验收标准.md](../LiteInfer%20开发%20WBS、里程碑排期与验收标准.md)（规划真源，冻结修改；推进变化只改看板）
V1.1 增补（RAM / VRAM / SSD 协同）：[..\\LiteInfer 开发 WBS、里程碑排期与验收标准2.md](../LiteInfer%20开发%20WBS、里程碑排期与验收标准2.md)

当前阶段：**G0 环境与架构验证（进行中，依赖未锁定）+ WBS-1 工程骨架（代码已落地，待逐条验收）**。
第一周执行清单见基线第 17 节；已落地内容与本地实测结果见 [docs/wbs-status.md](docs/wbs-status.md)。

## 快速开始（骨架期）

```bash
# 1) 进程内 SDK（不依赖 HTTP 服务）
python -c "from liteinfer import LiteInfer; \
li=LiteInfer(); \
tid=li.submit('echo', payload={'text':'hi'}); \
print(li.wait(tid, timeout_s=5).status)"

# 2) 独立 HTTP 服务 + CLI
python -m liteinfer.cli serve --port 8000          # 另开一个终端
python -m liteinfer.cli infer --kind echo --payload '{"text":"hi"}'
python -m liteinfer.cli task list
python -m liteinfer.cli model list
python -m liteinfer.cli doctor                     # 环境体检（不产出 GPU 结论）
```

`echo` 是占位执行器：它不加载任何模型，**不代表推理能力已具备**。
真实后端（SGLang / 训练执行路径）在 WBS-2 / WBS-5 接入。

HTTP 契约（标准库实现，见 [ADR-0002](docs/adr/0002-http-transport-stdlib.md)）：

```text
GET  /healthz                  200  服务健康 + 队列深度
GET  /v1/models                200  适配器清单（WBS-2 接入前恒为空）
GET  /v1/metrics               200  进程内指标快照
POST /v1/tasks                 201  提交任务（超队列上限 → 429，不进后台）
GET  /v1/tasks                 200  列表（可按 status / tenant / kind 过滤）
GET  /v1/tasks/{id}            200 / 404
POST /v1/tasks/{id}/cancel     202  两阶段回执 {accepted, completed, status}
```

## 目录速览

```text
liteinfer/
├── pyproject.toml            # 打包与工具配置（src layout）
├── src/liteinfer/            # 代码层（当前为骨架占位，见下）
├── tests/                    # unit / integration / gpu / regression / chaos
├── docs/                     # architecture / adr / runbooks / benchmarks + 安装与复现文档
├── env/                      # 环境与基线证书：硬件报告、兼容矩阵、版本台账、基线数据
└── deploy/                   # Dockerfile、compose、doctor / verify 脚本
```

## 代码层（src layout）

| 目录 | 职责 | 阶段 |
|---|---|---|
| `core/` | 领域模型、错误码、任务状态、配置校验 | WBS-1（已实现） |
| `runtime/` | 任务服务、资源租约、执行器协议 | WBS-1（已实现）/ WBS-3 |
| `observability/` | 结构化日志、指标注册表 | WBS-1（已实现）/ WBS-3 |
| `api/` | HTTP 请求与响应（标准库） | WBS-1（已实现） |
| `cli/` | 命令行 | WBS-1（已实现） |
| `client.py` | 进程内 SDK | WBS-1（已实现） |
| `adapters/` | 版本化模型适配器 | WBS-2（占位） |
| `backends/` | SGLang、PyTorch 等后端封装 | WBS-1/2（占位） |
| `memory/` | 显存计量、缓存、Swap | WBS-3/4（占位） |
| `training/` | 训练公共接口与训练器 | WBS-5/6（占位） |
| `weights/` | 权重与 LoRA 生命周期 | WBS-2/5（占位） |
| `scheduler/` | 队列、QoS、背压 | WBS-3（占位） |

首版纪律：`core/` 不直接依赖 SGLang、具体 PyTorch 模型类或 Web 框架；后端差异通过接口暴露。

## 依赖策略

不要在未运行验证前写死「某组最新版本号」。运行时依赖（Python / PyTorch / CUDA Runtime / SGLang / Triton）必须在
`env/compatibility-matrix.md` 完成锁定后才写入 `pyproject.toml`；当前 `[project].dependencies` 故意为空。

Python 依赖用 `uv.lock` 锁定，容器基础镜像固定标签（GPU 镜像另记录 digest）；CUDA 扩展还需锁源码版本、编译参数、
目标架构与构建产物哈希。

正式 GPU 验收环境：**Linux 容器 / WSL2 + 锁定的 Linux 容器镜像**。Windows 主机只用于 IDE、Git、客户端与文档。

## 第一周执行清单

- [ ] 建立 LiteInfer 仓库和项目骨架（本步已完成：pyproject / docs / tests / env / deploy / ADR 模板）
- [ ] 采集 A5000 环境基线 → 运行 `verify_gpu.py` / `verify_dependencies.py`，结果回填 `env/hardware-report.json`（GPU 段已初采，待复核 WSL2/Docker）
- [ ] 固定一套可工作的依赖组合 → `env/compatibility-matrix.md`
- [ ] 跑通 LLM 独立推理 → `docs/benchmarks/llm-baseline.md`
- [ ] 跑通 SDXL 独立推理 → `docs/benchmarks/sdxl-baseline.md`
- [ ] 验证 LLM LoRA 训练路径 → `docs/benchmarks/lora-train-probe.md`
- [ ] 提交第一份 ADR：`docs/adr/0001-*.md`

第一周不要开始实现完整 QoS 调度器、Swap 系统或通用训练引擎。

## 校验

```bash
# 代码侧（Windows 主机即可跑，只证明纯逻辑链路）
python -m pytest -m unit -q          # 42 passed
python -m pytest -m integration -q   # 真实起服务的 HTTP / CLI / SDK 用例
python -m ruff check .
python -m mypy                       # strict

# GPU 侧（只在 WSL2 Ubuntu-24.04 正式环境执行，见 ADR-0001）
python deploy/scripts/verify_dependencies.py     # 环境体检（Python / 工具 / 依赖差异）
python deploy/scripts/verify_gpu.py              # GPU 设备与显存快照
bash deploy/scripts/doctor.sh                    # 组合体检（Linux / WSL2 下）
bash deploy/scripts/bootstrap_wsl.sh --with-deps # WSL2 引导（虚拟环境 + 版本快照）
```

`tests/gpu/`、`tests/regression/`、`tests/chaos/` 仍为空：需要真实 GPU 与基线数据后才能写。
CI 定义在 `.github/workflows/ci.yml`（尚未在任何 runner 上实跑）。
