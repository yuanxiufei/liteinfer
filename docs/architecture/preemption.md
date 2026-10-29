# 安全抢占（WBS-3.9）

基线 §6.2 与 WBS-3.9 的要求是：**抢占只能在声明的安全点执行**，并且要有恢复证据。
本文件写清楚「做了什么」「明确没做什么」「怎么用」。

## 口径（三条硬纪律）

1. **只在声明了安全点的执行器上受理。**
   执行器协议新增 `supports_preemption`（默认 `False`）——
   **目前不对任何任务类型开放抢占**：内置 `EchoExecutor` / `FunctionExecutor` /
   `BlockingExecutor` 全部声明 `False`，对它们调用 `preempt()` 直接报
   `conflict`（HTTP 409），任务不会被偷偷暂停。
2. **状态由执行器自己交，服务端不猜。**
   执行器在可安全停下的位置调用 `context.checkpoint(state)`；
   收到抢占请求就抛 `TaskPreempted`，`state` 原样落到 `record.preempt["state"]`。
   服务端只做搬运，**不解释、不补全、不猜测** state 的内容。
3. **没有状态就不许叫"续跑"。**
   `resume()` 在没有安全点状态时报 `conflict` 并提示重新提交——
   绝不"从头重跑一遍然后说成恢复"。

## 状态机

```text
RUNNING ──preempt:requested──▶ PREEMPTING ──preempt:safepoint──▶ PREEMPTED
   │                              │                                │
   └── 执行器先跑完 → SUCCEEDED ───┘                                │
                                                                   │
                                       resume ──▶ QUEUED ──▶ 从断点接着跑
                                       放弃   ──▶ CANCELLED / FAILED / RECLAIMING
```

- `PREEMPTING` = 抢占已受理，执行器还没走到安全点。
- `PREEMPTED` = 已停在安全点、状态已存、资源已还，**不是终态**。
- 两者都计入 `NEEDS_RECOVERY`：进程退出后必须显式恢复或回收，
  不能默认"它还在跑"（资源其实已经还了）。

抢占请求发出后执行器仍可能先跑完——所以 `PREEMPTING → SUCCEEDED` 是合法迁移，
此时回执 `completed=False`：抢晚了就是没抢到，如实报。

## 抢占时做什么（顺序不能反）

```text
1) 存状态   record.preempt = {"state": ..., "at": ..., "rounds": n+1}
2) 还资源   租约释放 + KV 缓存预算归还（抢占的意义就是腾资源，不等终态）
3) 进状态   → PREEMPTED
```

`rounds` 记录被抢占过几次。测试验证过：抢占一次后 `resumed_from` 等于断点，
再抢一次 `rounds=2` 且断点继续往前，恢复后仍能跑完全部步数。

## 接口

| 层 | 抢占 | 恢复 |
|---|---|---|
| 领域 | `TaskService.preempt(task_id, timeout_s=5.0) -> PreemptAck` | `TaskService.resume(task_id)` |
| HTTP | `POST /v1/tasks/{id}/preempt` | `POST /v1/tasks/{id}/resume` |
| CLI | `liteinfer task preempt <id>` | `liteinfer task resume <id>` |
| SDK | `client.preempt(id)` | `client.resume(id)` |

`PreemptAck` 与 `CancelAck` 同口径：三件事分开报——

- `accepted`：请求被受理；
- `completed`：真的停在安全点（执行器先跑完则为 `False`）；
- `checkpoint`：有没有拿到可恢复状态。

等执行器到安全点超时报 `timeout`（504），不会因为"催不动"就把它强杀。

## 明确没做

- **没有强制抢占**：不杀线程、不打断 CUDA 工作。执行器不到安全点就抢不动，
  只会在超时后告诉你"没抢到"。
- **没有自动抢占策略**：系统不会自己决定"谁该被抢占"（那需要调度器按优先级/配额
  主动发起，属于后续工作）。当前只能由调用方显式发起。
- **没有真实后端可抢占**：`CheckpointExecutor` 是骨架/验证用执行器，
  用来打通「分 N 步 → 安全点交状态 → 从断点续跑」这条链路，
  **不能当作推理或训练任务已可抢占的证据**。

## 测试

- `tests/unit/test_preempt.py`：状态迁移白名单、PREEMPTED 属 NEEDS_RECOVERY、
  `checkpoint()` 未请求时静默 / 请求时抛 `TaskPreempted` 并带状态、
  `resume_state` 传递。
- `tests/integration/test_preempt_flow.py`：抢占后资源（租约 + KV 预算）归还、
  从断点续跑且 `resumed_from` 等于断点、二次抢占 `rounds=2`、
  未声明安全点被拒且任务没被偷偷暂停、排队/终态任务拒绝抢占、
  无状态不许恢复、PREEMPTED 留在 `unfinished()` 且 `needs_recovery=True`。
- `tests/integration/test_http_api.py`：HTTP 侧抢占被拒报 409（HTTP 层不改语义）。
