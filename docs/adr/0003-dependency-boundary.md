# ADR-0003：依赖边界（禁止第三方云端 API，第三方库按分级可选）

- 状态：proposed（待评审；WBS-0.2 锁定前先定边界，避免锁完才发现装了不该装的东西）
- 日期：2026-10-29
- 相关：ADR-0001（正式 GPU 环境）、ADR-0002（HTTP 传输层用标准库）、WBS-0.2（依赖锁定）

## 背景

WBS-0.2 要锁定依赖。在锁之前必须先回答一个问题：本项目允许依赖什么？
混淆点在于「第三方 API」和「第三方库」是两件事，必须分开定：

- **第三方 API**：走网络调用别人的服务（OpenAI、Anthropic、云端推理、模型仓库在运行期拉取）。
- **第三方库**：装在本机、本地进程内执行（torch、sglang、transformers、peft、diffusers、triton）。

## 决策

### 1. 禁止第三方云端 API（硬规则，无例外开关）

推理、训练、权重加载、算子编译全部在本机完成，**运行期不得发起任何外网调用**。
任何"调用外部服务"的需求都要单独开 ADR，并在配置里显式开关 + 默认关闭。

理由：本项目卖点之一就是数据不出本机；一旦运行期联网，"本地推理框架"的口径就失效了，
而且会让验收结果依赖外部服务的可用性与版本，无法复现。

### 2. 核心层零第三方依赖（已事实成立，做成机器判据）

`core` / `runtime` / `observability` / `api` / `cli` / `client` 只用 Python 标准库。
HTTP 服务用 `http.server`（ADR-0002），日志与指标自研。
这条由 `tests/unit/test_dependency_boundary.py` 用 AST 扫描守护，不靠人记得。

### 3. 第三方库只能进适配器 / 后端层，且按能力分级可选

| 级别 | 能力 | 涉及的库 | 缺失时行为 |
|---|---|---|---|
| L0 核心 | 任务、队列、状态机、资源租约、HTTP/CLI/SDK | 无（标准库） | 始终可用 |
| L1 推理 | LLM 推理 | torch + sglang（+ flashinfer / cuda-python） | 对应 kind 报 `unsupported`，核心不受影响 |
| L2 训练 | LoRA 微调 | torch + peft + transformers | 同上 |
| L3 扩散 | 扩散训练 | diffusers | 同上 |
| L4 算子 | 自定义内核 | triton | 同上 |

缺失某个级别 = 关闭对应能力，**不是整体装不起来**。
因此 `pyproject.toml` 的 `dependencies` 保持为空，各级别以 optional-dependencies（extras）提供；
具体版本等 WBS-0.2 在 WSL2 实测通过后再写，不在未实测时预填。

### 4. 依赖方向单向

`adapters/`、`backends/`、`training/`、`memory/` 可以依赖第三方；
`core/`、`runtime/`、`api/`、`cli/`、`client.py` **不得**反向依赖它们。
领域层只认协议（`Executor`、`资源采样接口`），不认具体实现。

### 5. 权重与模型的来源

只允许本地路径，或显式配置的可信仓库；**运行期不得静默联网拉取**。
下载行为必须是显式命令，且产物要能被校验。

## 影响

- 好处：验收结果完全可复现，不受外部服务波动影响；核心包极小、易安装、易审计。
- 代价：模型权重、算子生态要自己管；不能顺手用云端 API 兜底。
- 与 WBS 的关系：WBS-2（适配器）、WBS-5（训练）、WBS-6（扩散）都要按此分级落地；
  新增任何第三方依赖都要回到本 ADR 复核级别归属。

## 已有证据（截至本 ADR 提交时）

- `pyproject.toml` 的 `dependencies = []`；本地 venv 只有 dev 工具（pytest / ruff / mypy）。
- 源码中出现的 `http(s)://` 只有本机回环地址（`127.0.0.1:8000`、`http://{host}:{port}`）；
  `deploy/scripts/fetch_references.py` 里的 GitHub 地址仅用于开发期克隆参考源码，不参与运行期。
- 上述两条已固化为 `tests/unit/test_dependency_boundary.py`。
