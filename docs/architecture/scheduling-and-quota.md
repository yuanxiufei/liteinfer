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

## 两类缓存（3.5 / 3.6）

新增配置段 `cache`，**两类预算分开**（KV 按 token，张量特征按字节），不可互换、不可合并：

```json
{"cache": {"kv_tokens": 200000, "tensor_bytes": 4294967296}}
```

- **KV 缓存**（`runtime/cache.py::KVCacheBudget`）：预算、`reserve()` 调度许可、
  token 占用/命中/淘汰/回收计数、`owners()` 台账、`assert_no_leak()`。
  任务在 payload 里声明 `kv_tokens` 才占位；**预算不够就在开跑前拒绝**（503），
  任务终态（含取消/失败）一律归还。
- **张量特征缓存**（`TensorFeatureCache`）：容量上限、LRU 淘汰、
  `pin/unpin` 保护在途对象、淘汰回调 `on_evict`、**有界淘汰日志**（100 条）。
  装不下时先淘汰自己的 LRU；剩下的都被 pin 住就拒绝，并把 put 回滚（不留"放进去一半"的中间态）。

**不是物理显存硬隔离**：占用值是调用方上报的**记账值**，真实显存计量要等 3.4 的 NVML。

## 资源计量（3.4）

三类都有真实通路，`runtime/meter.py` 按「当前环境能测什么就装什么」组装
（`build_meter()`）：

| 类别 | 采样器 | 通路 | 口径 |
|---|---|---|---|
| 显存（GPU） | `NvmlMeter` | `pynvml` → 否则 `nvidia-smi --query-gpu=memory.used` | **整卡已用**显存，多卡取最大值 |
| 内存（CPU） | `ProcMeter` | Linux `/proc` VmRSS · Windows `psapi.GetProcessMemoryInfo` · macOS `resource` | 本进程常驻内存 |
| 磁盘（SSD） | `DiskMeter` | `shutil.disk_usage` | **整块盘**已用字节 |

三条纪律：

- **只报真测到的**：缺硬件或缺依赖 → `supported=False`，样本 `source` 标 `unavailable`，
  报告里 `measured=False`。**「没有数据」和「测得 0」必须能区分**，不许拿 0 填空。
- **三类分开记、分开报，不相加**：`CompositeMeter.report()` 逐类给
  `{bytes, source, measured}`；`/v1/resources` 与 `metrics()` 都按这个形状暴露。
- **逐类容错**：GPU 测不到不会把内存和磁盘一起拖垮，失败记在 `failures` 上。

没做的：本进程显存占用（要 `nvmlDeviceGetComputeRunningProcesses`，依赖锁定后再接）、
GPU 利用率、按卡拆分。

实测（本机 Windows + RTX A5000，驱动 610.88）：三类同时出数
`vram 1.05 GiB / ram 26.6 MB / ssd 1.75 TB`，来源
`nvml:nvidia-smi+win:WorkingSetSize+disk_usage`。
正式环境的 GPU 验收仍按 ADR-0001 在 WSL2 上做（Windows 主机不参与正式验收）。

## 已落地与未落地

| 任务 | 状态 | 说明 |
|---|---|---|
| 3.1 队列上限 | 已完成（WBS-1） | 全局 / kind / tenant 三档，超限 429 且不进后台 |
| 3.2 背压与限流 | 已完成（WBS-1） | 快速拒绝 + 稳定错误码 |
| 3.3 静态配额 | **本轮完成** | 预算 / 保留空间 / 并发上限入配置并生效 |
| 3.4 资源计量 | **本轮完成（三类通路）** | 显存（pynvml / nvidia-smi）· 内存（Linux /proc、Windows psapi、macOS resource）· 磁盘（disk_usage）都能跑；测不到标 `measured=False`；正式 GPU 验收仍在 WSL2 做 |
| 3.5 KV 缓存配额 | **本轮完成** | 预算 + token 统计 + 调度许可 + 归还台账；后端真实缓存块仍由后端自己管 |
| 3.6 张量特征缓存 | **本轮完成** | 容量上限 + LRU + pin/unpin + 淘汰回调与有界淘汰日志 |
| 3.7 资源租约 | 已完成（WBS-1） | 本轮加了借用台账 |
| 3.8 弹性借用 | **本轮完成** | 只借超出部分 + 借用者可查 |
| 3.9 安全抢占 | 未落地 | 目前只有「取消两阶段回执」，未对任何类型开放抢占 |
| 3.10 优先级调度 | **本轮完成** | 类别 > 优先级 > FIFO + 租户公平性 |

## 验证

- `tests/unit/test_scheduler.py`：分类、类别次序、同级优先级、FIFO、公平性让位、单租户不卡死、关闭唤醒
- `tests/unit/test_quota.py`：0=不设限、保留空间扣除、超配额拒绝、只借超出部分、借用随租约归还
- `tests/unit/test_cache.py`：KV 预算拒绝/归还/台账/不泄漏；张量 LRU 淘汰、pin 保护、put 回滚、淘汰日志有界
- `tests/unit/test_meter.py`：proc 解析、测不到就 `unsupported`、三类资源不合并
- `tests/integration/test_scheduling_flow.py`：真实服务里「训练先提交但推理先出队」、公平性让位、
  `max_inflight=2` 时最多 2 个并发、队列深度暴露调度统计
- `tests/integration/test_cache_flow.py`：并发下 KV 预算不足在开跑前拒绝、终态与取消均归还预算、
  两类缓存指标独立、释放证据里带真实采样

**一处实测到的时序**：任务**先落终态、再落回收证据**——`wait()` 返回时
`record.reclaim` 可能还没写好，观测释放证据要等一下（集成用例里用 `_reclaim_of` 轮询）。

**尚未验证**：G2 里所有需要压力测试 / 60 分钟稳定性 / 显存不增长的条目（需 GPU 与基线数据）。
