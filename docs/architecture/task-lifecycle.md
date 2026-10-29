# 任务生命周期

> 真源：`src/liteinfer/core/state.py`（迁移白名单）与 `src/liteinfer/runtime/service.py`（触发者）。
> 两者不一致时以代码为准，并回来改本文。

## 状态

```text
SUBMITTED ──► QUEUED ──► RUNNING ──► SUCCEEDED
                │           │
                │           ├──────► FAILED
                │           │
                │           └──► CANCELLING ──► CANCELLED
                │                     │
                │                     └────────► SUCCEEDED / FAILED
                └──► CANCELLING ──► CANCELLED
                └──► FAILED

RUNNING / CANCELLING ──► RECLAIMING ──► RECLAIMED   （进程退出后的恢复与回收）
```

终态：`succeeded` / `failed` / `cancelled` / `reclaimed`。
非终态且可能残留：`running` / `cancelling` / `reclaiming`（`NEEDS_RECOVERY`）。

## 迁移与触发者

| 迁移 | 触发者 | 说明 |
|---|---|---|
| `SUBMITTED → QUEUED` | `TaskService.submit` | 通过队列上限检查后入队；超限直接抛 `QUEUE_FULL`，**不进后台** |
| `QUEUED → RUNNING` | 工作线程 `_run` | 出队即尝试进入运行；已被取消则跳过 |
| `RUNNING → SUCCEEDED` | 工作线程 | 执行器正常返回，结果写入 `record.result` |
| `RUNNING → FAILED` | 工作线程 | 执行器抛异常或租约获取失败；`record.error` 保留原始错误码 |
| `RUNNING → CANCELLING` | `TaskService.cancel` | 仅当执行器 `supports_cancellation` 为 True |
| `CANCELLING → CANCELLED` | 工作线程 / `cancel` | 执行器在安全点抛出 `TaskCancelled`，或排队阶段直接取消 |
| `CANCELLING → SUCCEEDED/FAILED` | 工作线程 | 已接受取消但执行器仍跑完：如实记录，不伪装成取消 |
| `RUNNING/CANCELLING → RECLAIMING` | `mark_for_recovery` | 进程重启或 `close()` 时把悬空任务显式标记 |
| `RECLAIMING → RECLAIMED` | `reclaim` | 释放租约并写入回收记录 `record.reclaim` |

非法迁移一律抛 `CONFLICT`（HTTP 409），**不允许静默改写状态**。

## 取消语义（两阶段回执）

```text
cancel(task_id) → CancelAck{accepted, completed, status}

QUEUED  阶段：accepted=True, completed=True   （立即完成）
RUNNING 且支持安全点：accepted=True, completed=False → 执行器在安全点退出后转 CANCELLED
RUNNING 且不支持安全点：抛 CONFLICT（拒绝），不撒谎说"已取消"
终态：CANCELLED → accepted=True, completed=True；其他终态 → 抛 CONFLICT
```

运行中的 GPU 内核不一定能立即中止：这是「已接受取消」与「已完成取消」必须分开报的原因。

## 幂等与资源回收

- 客户端重试靠 `client_token`：同一 token 只产生一个任务（`TaskService._tokens`）。
- 每个终态任务都有 `reclaim` 记录：`{"released": true, "lease_id": ..., "released_sample": ...}`；
  未持有租约时写 `{"released": true, "lease_id": null, "note": "no_lease"}`，不允许回收记录凭空消失。
- 租约是**逻辑配额**（`runtime/lease.py`），不是 GPU 显存硬隔离；真实显存采样在 WBS-3 通过 `ResourceMeter` 接入。

## 验证方式

```bash
pytest -m unit          # 状态机、背压、取消、回收的迁移断言
pytest -m integration   # SDK / HTTP / CLI 的真实链路
```

相关测试：`tests/unit/test_state.py`、`tests/unit/test_service.py`、`tests/integration/test_http_api.py`。
