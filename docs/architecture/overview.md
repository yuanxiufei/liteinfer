# 分层架构概览

> 本文描述**已落地**的骨架（WBS-1 + WBS-2 部分），不是目标架构的全部。随 WBS-3 / 5 推进更新。
> 适配器与权重的细节见 [adapters-and-weights.md](adapters-and-weights.md)。

## 分层

```text
┌──────────────────────────────────────────────────────────────┐
│ 交付形态层   cli/（命令行）        api/（HTTP 服务）           │
│              └── 两者都调用同一套核心领域服务，不各写一份逻辑   │
├──────────────────────────────────────────────────────────────┤
│ SDK 层       client.py（进程内 LiteInfer / session）           │
├──────────────────────────────────────────────────────────────┤
│ 运行时层     runtime/  service（生命周期·队列·背压·取消·回收） │
│                        scheduler（类别/优先级/公平性出队）      │
│                        executor（执行器协议）                  │
│                        lease（资源租约、配额与弹性借用台账）     │
├──────────────────────────────────────────────────────────────┤
│ 领域层       core/    models / state / config / errors / ids   │
├──────────────────────────────────────────────────────────────┤
│ 适配器层     adapters/ 协议 · 注册中心 · 兼容性                │
│              weights/   清单校验 · 引用计数生命周期            │
│              └── 由 bootstrap.py（组合根）装配，runtime 只认端口│
├──────────────────────────────────────────────────────────────┤
│ 横切         observability/  结构化日志（请求/任务 ID）+ 指标   │
└──────────────────────────────────────────────────────────────┘
  尚未接入（WBS-3 / 5）：backends/ training/ memory/（双缓存与 Swap）
                        QoS 时延承诺、KV 与张量缓存（3.4–3.6）
```

出队顺序与配额口径见 [scheduling-and-quota.md](scheduling-and-quota.md)。

## 依赖方向（硬纪律）

1. `core/` **不得**导入 SGLang、具体 PyTorch 模型类或任何 Web 框架；它只描述领域事实。
2. `runtime/` 依赖 `core/` 与 `observability/`，**不依赖** `api/` 或 `cli/`。
3. `api/` 与 `cli/` 只做协议转换：HTTP 控制器与 argparse 回调里不允许出现业务判断。
4. 后端差异通过 `runtime/executor.py::Executor` 协议暴露，替换后端不得改动领域层。
5. 依赖方向只允许自上而下；任何反向 import 视为缺陷。
6. 「`core` / `runtime` / `api` / `cli` 不得 import `adapters`"由
   `tests/unit/test_dependency_boundary.py` 与 `runtime/ports.py` 共同保证；
   唯一例外是组合根 `bootstrap.py`，它负责装配并把适配器注入运行时。

## 模块现状

| 模块 | 状态 | 说明 |
|---|---|---|
| `core/errors.py` | 已实现 | 跨层错误码 + HTTP 状态码映射（唯一真源） |
| `core/state.py` | 已实现 | 状态迁移白名单，非法迁移抛 `CONFLICT` |
| `core/models.py` | 已实现 | 任务 / 模型 / 权重 / 适配器 / 预算引用 |
| `core/config.py` | 已实现 | 配置校验，未知键拒绝（不静默忽略） |
| `observability/` | 已实现 | JSON 日志 + request/task ID 关联 + 进程内指标 |
| `runtime/lease.py` | 已实现 | 逻辑配额记账；**不等于显存硬隔离** |
| `runtime/executor.py` | 已实现 | 执行器协议 + `echo` 占位执行器 |
| `runtime/scheduler.py` | 已实现 | 类别 > 优先级 > FIFO 出队 + 租户公平性（WBS-3.10） |
| `runtime/cache.py` | 已实现 | 两类缓存：KV 预算与调度许可、张量特征 LRU（WBS-3.5/3.6，**非物理隔离**） |
| `runtime/meter.py` | 已实现 | 三类采样：显存（pynvml / nvidia-smi）、内存（Linux /proc、Windows psapi、macOS resource）、磁盘（disk_usage）；测不到标 `measured=False`（WBS-3.4） |
| `runtime/executor.py` 的 `supports_preemption` / `checkpoint()` | 已实现 | 安全抢占只在声明安全点的执行器上受理，默认不开放（WBS-3.9） |
| `runtime/service.py` | 已实现 | 队列上限、背压、幂等、取消两阶段回执、回收、并发上限 |
| `client.py` | 已实现 | 进程内 SDK，不依赖 HTTP |
| `api/` | 已实现 | 标准库 HTTP 服务（见 ADR-0002） |
| `cli/` | 已实现 | doctor / serve / model list / infer / task * |
| `adapters/` | 部分实现 | 协议 + 注册中心 + 兼容性检查（WBS-2.1/2.2/2.3）；LLM / SDXL 适配器待依赖锁定 |
| `core/config.py` | 已实现 | 配置校验，未知键拒绝（不静默忽略）；`queue`/`quota` 段含调度与配额 |
| `weights/` | 已实现 | 清单校验（损坏绝不判 ok）+ 引用计数生命周期与释放证据（WBS-2.6/2.7） |
| `bootstrap.py` | 已实现 | 组合根：装配适配器与执行器，注入运行时端口 |

## 关键边界（不可写成别的意思）

- **显存配额 ≠ 硬隔离**：若底层组件绕过 LiteInfer 的接口直接申请显存，LiteInfer 只能记账、告警和拒绝新租约。
- **`echo` 执行器不是能力证据**：它不加载任何模型；推理能力要等 LLM / SDXL 适配器接入后才能宣称
  （当前唯一可调用的真实适配器是 `weights.verify`，它只做权重校验，不做推理）。
- **取消是两阶段**：`accepted`（已接受）与 `completed`（已完成）分开报；只对声明 `supports_cancellation` 的执行器开放运行中的取消。
- **调度不是 QoS**：优先队列只决定「下一个取谁」，不保证时延，也不抢占已在跑的任务。
- **配额 0 = 不设限**：真实数字必须来自 WBS-0.2 锁定后的 GPU 实测，未实测前不得填假值。
- **配置项不许只计数不生效**：`queue.max_inflight` 曾一度不限制并发（缺陷），现在它就是 worker 数。
