# 分层架构概览

> 本文描述**已落地**的骨架（WBS-1），不是目标架构的全部。随 WBS-2 / 3 / 5 推进更新。

## 分层

```text
┌──────────────────────────────────────────────────────────────┐
│ 交付形态层   cli/（命令行）        api/（HTTP 服务）           │
│              └── 两者都调用同一套核心领域服务，不各写一份逻辑   │
├──────────────────────────────────────────────────────────────┤
│ SDK 层       client.py（进程内 LiteInfer / session）           │
├──────────────────────────────────────────────────────────────┤
│ 运行时层     runtime/  service（生命周期·队列·背压·取消·回收） │
│                        executor（执行器协议）                  │
│                        lease（资源租约与记账）                 │
├──────────────────────────────────────────────────────────────┤
│ 领域层       core/    models / state / config / errors / ids   │
├──────────────────────────────────────────────────────────────┤
│ 横切         observability/  结构化日志（请求/任务 ID）+ 指标   │
└──────────────────────────────────────────────────────────────┘
  尚未接入（WBS-2 / 3 / 5）：adapters/ backends/ weights/ training/
                            memory/（双缓存与 Swap）scheduler/（QoS）
```

## 依赖方向（硬纪律）

1. `core/` **不得**导入 SGLang、具体 PyTorch 模型类或任何 Web 框架；它只描述领域事实。
2. `runtime/` 依赖 `core/` 与 `observability/`，**不依赖** `api/` 或 `cli/`。
3. `api/` 与 `cli/` 只做协议转换：HTTP 控制器与 argparse 回调里不允许出现业务判断。
4. 后端差异通过 `runtime/executor.py::Executor` 协议暴露，替换后端不得改动领域层。
5. 依赖方向只允许自上而下；任何反向 import 视为缺陷。

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
| `runtime/service.py` | 已实现 | 队列上限、背压、幂等、取消两阶段回执、回收 |
| `client.py` | 已实现 | 进程内 SDK，不依赖 HTTP |
| `api/` | 已实现 | 标准库 HTTP 服务（见 ADR-0002） |
| `cli/` | 已实现 | doctor / serve / model list / infer / task * |

## 关键边界（不可写成别的意思）

- **显存配额 ≠ 硬隔离**：若底层组件绕过 LiteInfer 的接口直接申请显存，LiteInfer 只能记账、告警和拒绝新租约。
- **`echo` 执行器不是能力证据**：它不加载任何模型；推理能力要等 WBS-2 接入适配器后才能宣称。
- **取消是两阶段**：`accepted`（已接受）与 `completed`（已完成）分开报；只对声明 `supports_cancellation` 的执行器开放运行中的取消。
