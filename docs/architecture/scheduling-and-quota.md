# 调度、配额与弹性借用（WBS-3 部分）

> 描述**已落地**部分（3.1 / 3.2 / 3.3 / 3.8 / 3.10，及 3.7 租约的沿用），未落地部分单独标注。
> 详细语义见源码 docstring；本文只讲口径与边界。

## 出队顺序（3.10）

`runtime/scheduler.py::TaskQueue` 决定「下一个取谁」，排序键是三元组：

```text
(类别次序, -显式优先级, 提交序号)
    │           │            └─ 同优先级按提交先后（FIFO）
    │           └─ TaskRequest.priority 大者优先
    └─ 推理 0 < 评估 1 < 训练 2
```

类别由 kind 判定：先看 `queue.kind_class` 显式映射，再按前缀规则
（`train/lora/finetune/sft/distill` → training，`eval/benchmark/validate/test/score` → eval），
**未知 kind 归 inference**。

**公平性**：`queue.fairness_limit=N` 时，同一 tenant 连续取走 N 个后必须让位给别的 tenant；
队列里只剩这一个 tenant 时按优先级继续（不会卡死）。`0` 表示不启用。
让位次数记在 `queue_depth()["scheduler"]["fairness_skips"]`。

**边界**：这只是「下一个取谁」。它**不是 QoS 承诺**——不保证时延，也不抢占已在跑的任务
（抢占是 3.9，只对声明安全点的执行器开放，目前尚未对任何任务类型开放）。

## 静态配额（3.3）

新增配置段 `quota`：

```json
{"quota": {"vram_bytes": 24000000000, "reserved_vram_bytes": 1000000000,
           "borrow_vram_bytes": 2000000000}}
```

| 字段 | 含义 |
|---|---|
| `{vram,ram,ssd}_bytes` | 该类资源的基础配额；**0 = 不设限**，不是"限额为零" |
| `reserved_*_bytes` | 不可动用的保留空间，从可用容量里扣掉 |
| `borrow_*_bytes` | 弹性借用上限（3.8），0 = 不允许借用 |
| `queue.max_inflight` | **并发上限 = worker 线程数**（不是只计数不生效） |
| `queue.fairness_limit` | 租户公平性限制 |
| `queue.kind_class` | kind → 类别的显式映射 |

配额流向：`QuotaConfig.usable()`（扣保留）→ `LeaseManager.capacity`；
`borrow_limit()` → `LeaseManager.borrow_limit`。

> **修掉的一处真缺陷**：`max_inflight` 曾经只被计数、从不限制——配置项存在但不生效，
> 比报错更危险。现在它就是 worker 数，超出的任务留在队列里排队。

## 弹性借用（3.8）

只借**超出基础配额的那部分**，不是把整个请求算成借用：

```text
capacity=100, 已用=60, 新请求=60  ⇒  超出 20  ⇒  借用 20（不是 60）
```

每条借用进台账 `LeaseManager.borrowers()`（lease_id / task_id / 数量 / 时刻），
租约释放时一并归还。借用额度用尽 → `resource_exhausted`（503），
错误 details 里带 `over` / `borrowed` / `borrow_limit`。

## 已落地与未落地

| 任务 | 状态 | 说明 |
|---|---|---|
| 3.1 队列上限 | 已完成（WBS-1） | 全局 / kind / tenant 三档，超限 429 且不进后台 |
| 3.2 背压与限流 | 已完成（WBS-1） | 快速拒绝 + 稳定错误码 |
| 3.3 静态配额 | **本轮完成** | 预算 / 保留空间 / 并发上限入配置并生效 |
| 3.4 资源计量 | 未落地 | 需要 NVML 与 GPU 实测（WSL2） |
| 3.5 KV 缓存配额 | 未开始 | 需后端缓存状态接入 |
| 3.6 张量特征缓存 | 未开始 | LRU 与对象生命周期 |
| 3.7 资源租约 | 已完成（WBS-1） | 本轮加了借用台账 |
| 3.8 弹性借用 | **本轮完成** | 只借超出部分 + 借用者可查 |
| 3.9 安全抢占 | 未落地 | 目前只有「取消两阶段回执」，未对任何类型开放抢占 |
| 3.10 优先级调度 | **本轮完成** | 类别 > 优先级 > FIFO + 租户公平性 |

## 验证

- `tests/unit/test_scheduler.py`：分类、类别次序、同级优先级、FIFO、公平性让位、单租户不卡死、关闭唤醒
- `tests/unit/test_quota.py`：0=不设限、保留空间扣除、超配额拒绝、只借超出部分、借用随租约归还
- `tests/integration/test_scheduling_flow.py`：真实服务里「训练先提交但推理先出队」、公平性让位、
  `max_inflight=2` 时最多 2 个并发、队列深度暴露调度统计

**尚未验证**：G2 里所有需要压力测试 / 60 分钟稳定性 / 显存不增长的条目（需 GPU 与基线数据）。
