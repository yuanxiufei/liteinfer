"""任务服务：生命周期、队列上限、背压、取消与回收（WBS-1.3 / WBS-3）。

口径：
1. 每个任务都有唯一 ID、明确终态、错误信息与资源回收记录；
2. 队列满时**快速拒绝**，不允许请求被悄悄塞进后台；
3. 客户端重试通过 `client_token` 幂等，不制造重复任务；
4. 取消区分「已接受取消」与「已完成取消」，且只对声明支持安全点的执行器开放；
5. 抢占（WBS-3.9）同理：只在执行器声明安全点时才受理，抢占后**先存状态、再还资源**，
   没有拿到安全点状态就不许说"可续跑"；
6. 开放抢占的门槛是 G2 的原话：**只对已实现并通过恢复测试的任务类型开放**——
   执行器自述支持不算，必须在 `preempt.verified_kinds` 登记并写出恢复测试证据。
   默认台账为空 ⇒ **默认不对任何任务类型开放**。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..core import (
    ErrorCode,
    LiteInferConfig,
    LiteInferError,
    ResourceBudget,
    TaskCancelled,
    TaskPreempted,
    TaskRecord,
    TaskRequest,
    TaskStatus,
    conflict,
    dependency_missing,
    invalid_argument,
    is_terminal,
    not_found,
    queue_full,
    request_from_mapping,
    timed_out,
    unsupported,
)
from ..core.models import TaskError
from ..observability import MetricRegistry, get_logger, log_event
from .cache import KVCacheBudget, TensorFeatureCache
from .executor import ExecutionContext, Executor, default_executor
from .lease import LeaseManager, ResourceLease, ResourceMeter, ResourceSample
from .meter import CompositeMeter
from .ports import AdapterInfo, AdapterLookup
from .preemption import PreemptionPlanner, rounds_of
from .scheduler import CLASS_ORDER, TaskQueue, classify_kind


@dataclass(frozen=True)
class CancelAck:
    """取消回执。`accepted` 与 `completed` 必须分开报，不许混成一个布尔。"""

    task_id: str
    accepted: bool
    completed: bool
    status: TaskStatus

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "accepted": self.accepted,
            "completed": self.completed,
            "status": self.status.value,
        }


@dataclass(frozen=True)
class PreemptAck:
    """抢占回执（WBS-3.9）。

    与 `CancelAck` 同样口径：`accepted`（请求被受理）与 `completed`（真的停在安全点）
    必须分开报；`checkpoint` 说明有没有拿到可恢复状态——
    **没拿到状态就不许说成"已暂停可续跑"**。
    """

    task_id: str
    accepted: bool
    completed: bool
    status: TaskStatus
    checkpoint: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "accepted": self.accepted,
            "completed": self.completed,
            "status": self.status.value,
            "checkpoint": self.checkpoint,
        }


class TaskService:
    """进程内任务服务。HTTP 层与 SDK 共用同一套实例语义。"""

    def __init__(
        self,
        config: LiteInferConfig | None = None,
        executor: Executor | None = None,
        *,
        lease_manager: LeaseManager | None = None,
        adapter_lookup: AdapterLookup | None = None,
        kv_cache: KVCacheBudget | None = None,
        tensor_cache: TensorFeatureCache | None = None,
        meter: ResourceMeter | None = None,
        start_worker: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config or LiteInferConfig()
        self._config.validate()
        self._executor = executor or default_executor()
        self._lease_manager = lease_manager or self._lease_manager_from_quota(
            self._config, meter
        )
        # 采样器留一份：资源采样要能单独查（WBS-3.4 的三类分别报）
        self._meter = meter
        self._adapter_lookup = adapter_lookup
        # 两类缓存预算分开（基线 §6.2）：KV 按 token，张量特征按字节，互不挪用
        self._kv_cache = kv_cache or KVCacheBudget(
            capacity_tokens=self._config.cache.kv_tokens
        )
        self._tensor_cache = tensor_cache or TensorFeatureCache(
            capacity_bytes=self._config.cache.tensor_bytes
        )
        self._clock = clock
        self._logger = get_logger("service")
        self._metrics = MetricRegistry()

        self._lock = threading.RLock()
        self._records: dict[str, TaskRecord] = {}
        self._tokens: dict[str, str] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._preempt_events: dict[str, threading.Event] = {}
        #: 抢占台账（WBS-3.9）：有界，谁抢了谁、拿到状态没有，事后必须能追问
        self._preempt_log: deque[dict[str, Any]] = deque(maxlen=100)
        self._preemption = PreemptionPlanner(
            policy=self._config.preempt, class_rank=self._class_rank
        )
        self._queued_total = 0
        self._queued_by_kind: dict[str, int] = {}
        self._queued_by_tenant: dict[str, int] = {}
        self._inflight = 0
        self._closed = False

        # 优先队列（WBS-3.10）：类别 > 显式优先级 > 提交序，带租户公平性限制
        self._queue = TaskQueue(
            class_of=self._config.queue.kind_class,
            fairness_limit=self._config.queue.fairness_limit,
        )
        self._stopping = threading.Event()
        self._workers: list[threading.Thread] = []
        if start_worker:
            self.start()

    @staticmethod
    def _lease_manager_from_quota(
        config: LiteInferConfig, meter: ResourceMeter | None = None
    ) -> LeaseManager:
        """配额来自 `QuotaConfig`：扣掉保留空间后的容量 + 弹性借用上限。"""
        quota = config.quota
        return LeaseManager(
            meter,
            capacity=ResourceBudget(**quota.usable()),
            borrow_limit=ResourceBudget(**quota.borrow_limit()),
        )

    def _class_rank(self, kind: str) -> int:
        """类别次序：推理 0 < 评估 1 < 训练 2，与出队顺序用同一套判定规则。"""
        return CLASS_ORDER.get(
            classify_kind(kind, self._config.queue.kind_class), len(CLASS_ORDER)
        )

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> None:
        """启动 worker。`max_inflight` 就是并发上限（= worker 数），不是只计数不生效。"""
        with self._lock:
            if any(worker.is_alive() for worker in self._workers):
                return
            self._closed = False
            self._stopping.clear()
            count = max(1, self._config.queue.max_inflight)
            for index in range(count):
                worker = threading.Thread(
                    target=self._loop, name=f"liteinfer-worker-{index}", daemon=True
                )
                self._workers.append(worker)
                worker.start()

    def close(self, *, timeout_s: float = 5.0, mark_unfinished: bool = True) -> None:
        """停止消费并把未终结任务标记为需要恢复（而不是悄悄丢掉）。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stopping.set()
            workers = list(self._workers)
            self._queue.close()
        for worker in workers:
            worker.join(timeout=timeout_s)
        if mark_unfinished:
            for record in self.unfinished():
                self.mark_for_recovery(record.task_id)
                self.reclaim(record.task_id)

    # ------------------------------------------------------------------ 提交

    def submit(self, request: TaskRequest) -> TaskRecord:
        if not isinstance(request, TaskRequest):
            raise invalid_argument("submit 需要 TaskRequest")
        if not request.kind.strip():
            raise invalid_argument("kind 不能为空")
        self._preflight(request)

        with self._lock:
            if self._closed:
                raise conflict("服务已关闭，不再接受新任务")
            token = request.client_token
            if token:
                existing_id = self._tokens.get(token)
                if existing_id is not None:
                    return self._records[existing_id]

            self._check_backpressure(request)
            record = TaskRecord(request=request)
            record.transition_to(TaskStatus.QUEUED, reason="submitted", clock=self._clock)
            self._records[record.task_id] = record
            if token:
                self._tokens[token] = record.task_id
            self._queued_total += 1
            self._queued_by_kind[request.kind] = self._queued_by_kind.get(request.kind, 0) + 1
            self._queued_by_tenant[request.tenant] = (
                self._queued_by_tenant.get(request.tenant, 0) + 1
            )
            self._cancel_events[record.task_id] = threading.Event()
            self._preempt_events[record.task_id] = threading.Event()
            self._queue.push(record)

        self._metrics.counter("tasks.submitted").inc()
        self._metrics.gauge("queue.depth").set(float(self._queued_total))
        log_event(
            self._logger,
            "task.submitted",
            task_id=record.task_id,
            kind=request.kind,
            tenant=request.tenant,
        )
        return record

    def _preflight(self, request: TaskRequest) -> None:
        """启动前门禁（G2 前置验收条件）。

        该 kind 只要注册了适配器，就必须在这里把「缺失依赖 / 不兼容」报出来，
        不能等到执行到一半才发现。未注册 kind 不干预，交给执行器按 unsupported 处理。
        """
        lookup = self._adapter_lookup
        if lookup is None:
            return
        info = lookup.find_for_kind(request.kind)
        if info is None:
            return
        if info.missing_dependencies:
            raise dependency_missing(
                f"任务类型 {request.kind} 依赖的适配器缺少可选依赖，无法启动",
                kind=request.kind,
                adapter_id=info.adapter_id,
                missing=list(info.missing_dependencies),
                hint="按 ADR-0003 安装对应 extras 后重试",
            )
        if not info.ready:
            raise unsupported(
                f"任务类型 {request.kind} 的适配器未通过兼容性检查",
                kind=request.kind,
                adapter_id=info.adapter_id,
                problems=list(info.problems),
            )

    def adapters(self) -> list[AdapterInfo]:
        """已注册适配器快照（给 HTTP `/v1/models` 与 CLI `model list`）。"""
        if self._adapter_lookup is None:
            return []
        return self._adapter_lookup.describe()

    def submit_mapping(self, data: Mapping[str, Any]) -> TaskRecord:
        return self.submit(request_from_mapping(data))

    def _check_backpressure(self, request: TaskRequest) -> None:
        limits = self._config.queue
        if self._queued_total + 1 > limits.max_queued:
            self._metrics.counter("tasks.rejected").inc()
            raise queue_full(
                "队列已满，拒绝新请求（请稍后重试）",
                limit=limits.max_queued,
                current=self._queued_total,
                scope="global",
            )
        kind_depth = self._queued_by_kind.get(request.kind, 0)
        if kind_depth + 1 > limits.max_queued_per_kind:
            self._metrics.counter("tasks.rejected").inc()
            raise queue_full(
                "该任务类型的排队量已达上限",
                limit=limits.max_queued_per_kind,
                current=kind_depth,
                scope="kind",
                kind=request.kind,
            )
        tenant_depth = self._queued_by_tenant.get(request.tenant, 0)
        if tenant_depth + 1 > limits.max_queued_per_tenant:
            self._metrics.counter("tasks.rejected").inc()
            raise queue_full(
                "该 tenant 的排队量已达上限",
                limit=limits.max_queued_per_tenant,
                current=tenant_depth,
                scope="tenant",
                tenant=request.tenant,
            )

    # ------------------------------------------------------------------ 查询

    def get(self, task_id: str) -> TaskRecord:
        with self._lock:
            record = self._records.get(task_id)
        if record is None:
            raise not_found("任务不存在", task_id=task_id)
        return record

    def wait(self, task_id: str, *, timeout_s: float | None = None) -> TaskRecord:
        record = self.get(task_id)
        if not record.done.wait(timeout=timeout_s):
            raise timed_out("等待任务终态超时", task_id=task_id, timeout_s=timeout_s)
        return record

    def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
        tenant: str | None = None,
        kind: str | None = None,
    ) -> list[TaskRecord]:
        with self._lock:
            records = list(self._records.values())
        return [
            record
            for record in records
            if (status is None or record.status == status)
            and (tenant is None or record.request.tenant == tenant)
            and (kind is None or record.request.kind == kind)
        ]

    def unfinished(self) -> list[TaskRecord]:
        return [record for record in self.list_tasks() if not record.is_terminal]

    def queue_depth(self) -> dict[str, Any]:
        with self._lock:
            depth = {
                "queued": self._queued_total,
                "inflight": self._inflight,
                # 停在安全点上等恢复的任务（WBS-3.9）：不是排队，也不是在跑
                "preempted": sum(
                    1 for record in self._records.values() if record.status is TaskStatus.PREEMPTED
                ),
                "by_kind": dict(self._queued_by_kind),
                "by_tenant": dict(self._queued_by_tenant),
            }
        depth["scheduler"] = self._queue.stats()
        return depth

    def resource_sample(self) -> dict[str, Any]:
        """三类资源分别采样（WBS-3.4）：显存 / 内存 / 磁盘，测不到就 `measured=False`。"""
        meter = self._meter
        sample = meter.sample() if meter is not None else ResourceSample(source="declared")
        if isinstance(meter, CompositeMeter):
            report = meter.report()
        else:
            measured = sample.source not in ("declared", "unavailable")
            report = {
                kind: {
                    "bytes": int(getattr(sample, field)),
                    "source": sample.source if measured else "unmeasured",
                    "measured": measured,
                }
                for kind, field in (
                    ("vram", "vram_bytes"),
                    ("ram", "ram_bytes"),
                    ("ssd", "ssd_bytes"),
                )
            }
        return {"sample": sample.to_dict(), "resources": report}

    def metrics(self) -> dict[str, Any]:
        snapshot = self._metrics.snapshot()
        snapshot["cache"] = self.cache_stats()
        snapshot["resources"] = self.resource_sample()
        return snapshot

    # ------------------------------------------------------------------ 取消 / 恢复

    def cancel(self, task_id: str) -> CancelAck:
        with self._lock:
            record = self.get(task_id)
            status = record.status

            if record.is_terminal:
                if status is TaskStatus.CANCELLED:
                    return CancelAck(task_id, accepted=True, completed=True, status=status)
                raise conflict("任务已进入终态，不能取消", task_id=task_id, status=status.value)

            record.cancel_requested = True

            if status in (TaskStatus.SUBMITTED, TaskStatus.QUEUED):
                record.transition_to(
                    TaskStatus.CANCELLING, reason="cancel:queued", clock=self._clock
                )
                record.transition_to(
                    TaskStatus.CANCELLED, reason="cancel:queued", clock=self._clock
                )
                self._metrics.counter("tasks.cancelled").inc()
                return CancelAck(task_id, accepted=True, completed=True, status=record.status)

            if status is TaskStatus.CANCELLING:
                return CancelAck(task_id, accepted=True, completed=False, status=status)

            if status is TaskStatus.RUNNING:
                if not self._executor.supports_cancellation:
                    raise conflict(
                        "该执行器未声明安全点取消能力，运行中的任务不接受取消",
                        task_id=task_id,
                        executor=type(self._executor).__name__,
                    )
                event = self._cancel_events.get(task_id)
                if event is not None:
                    event.set()
                record.transition_to(
                    TaskStatus.CANCELLING, reason="cancel:running", clock=self._clock
                )
                return CancelAck(task_id, accepted=True, completed=False, status=record.status)

            raise conflict("当前状态不支持取消", task_id=task_id, status=status.value)

    def preempt(
        self,
        task_id: str,
        *,
        timeout_s: float = 5.0,
        requester: str | None = None,
        reason: str = "manual",
    ) -> PreemptAck:
        """请求抢占：只在「执行器声明安全点 **且** 该类型已登记通过恢复测试」时受理，
        并等到它真的停下。

        三件事分开报：请求被受理（accepted）、已停在安全点（completed）、
        有没有拿到可恢复状态（checkpoint）。
        `requester` / `reason` 是台账字段：自动抢占会把「谁发起的」一并记进去。
        """
        record = self.get(task_id)
        if record.is_terminal:
            raise conflict("任务已进入终态，不能抢占", task_id=task_id, status=record.status.value)
        self._preempt_gate(record)

        with self._lock:
            status = record.status
            if status is TaskStatus.PREEMPTED:
                ack = PreemptAck(
                    task_id, True, True, status, checkpoint=record.preempt is not None
                )
                self._record_preemption(record, ack, requester=requester, reason=reason)
                return ack
            if status not in (TaskStatus.RUNNING, TaskStatus.PREEMPTING):
                raise conflict("当前状态不支持抢占", task_id=task_id, status=status.value)
            record.preempt_requested = True
            if status is TaskStatus.RUNNING:
                record.transition_to(
                    TaskStatus.PREEMPTING, reason="preempt:requested", clock=self._clock
                )
            event = self._preempt_events.get(task_id)
        if event is not None:
            event.set()

        deadline = self._clock() + timeout_s
        while self._clock() < deadline:
            current = self.get(task_id).status
            if current is TaskStatus.PREEMPTED:
                self._metrics.counter("tasks.preempted").inc()
                ack = PreemptAck(
                    task_id, True, True, current, checkpoint=self.get(task_id).preempt is not None
                )
                self._record_preemption(record, ack, requester=requester, reason=reason)
                return ack
            if is_terminal(current):
                # 执行器在走到安全点之前就跑完了：抢占请求来晚，就如实说没抢到
                ack = PreemptAck(task_id, True, False, current, checkpoint=False)
                self._record_preemption(record, ack, requester=requester, reason=reason)
                return ack
            time.sleep(0.01)
        raise timed_out(
            "等待执行器到达安全点超时",
            task_id=task_id,
            timeout_s=timeout_s,
            status=self.get(task_id).status.value,
        )

    def _preempt_gate(self, record: TaskRecord) -> None:
        """抢占准入（WBS-3.9 / G2）：四道门槛，缺一不可，拒绝理由必须可读。

        G2 的原话是「只对已实现并通过恢复测试的任务类型开放」，所以光执行器自述
        支持不够，还要在 `preempt.verified_kinds` 登记（并写出恢复测试证据）；
        轮次用尽也不再抢占——抢占是为了让更重要的任务先跑，不是为了让谁永远跑不完。
        """
        policy = self._config.preempt
        kind = record.request.kind
        task_id = record.task_id
        if not policy.enabled:
            raise conflict(
                "抢占未启用，运行中的任务不接受抢占（未对任何任务类型开放安全点抢占）",
                task_id=task_id,
                kind=kind,
                hint="preempt.enabled 默认 false，且默认台账为空；开放前必须登记恢复测试证据",
            )
        if not getattr(self._executor, "supports_preemption", False):
            raise conflict(
                "该执行器未声明安全点抢占能力，运行中的任务不接受抢占",
                task_id=task_id,
                executor=type(self._executor).__name__,
                hint="抢占只能在声明了安全点的执行器上开放（WBS-3.9）",
            )
        if not policy.eligible(kind):
            raise conflict(
                f"任务类型 {kind} 未登记为「已实现并通过恢复测试」，不允许抢占",
                task_id=task_id,
                kind=kind,
                verified=sorted(policy.verified_kinds),
                hint=(
                    "G2 要求只对通过恢复测试的类型开放；"
                    "登记时要在 preempt.verification 写取证路径"
                ),
            )
        rounds = rounds_of(record)
        if rounds >= policy.max_rounds:
            raise conflict(
                f"该任务已被抢占 {rounds} 次，达到 max_rounds 上限，不再接受抢占",
                task_id=task_id,
                kind=kind,
                rounds=rounds,
                max_rounds=policy.max_rounds,
                hint="抢占也会制造饥饿：轮次用尽的任务必须让它跑完，而不是反复踢它下去",
            )

    def _record_preemption(
        self,
        record: TaskRecord,
        ack: PreemptAck,
        *,
        requester: str | None,
        reason: str,
    ) -> None:
        """抢占台账（有界）：谁抢了谁、在第几轮、有没有真的拿到安全点状态。"""
        with self._lock:
            self._preempt_log.append(
                {
                    "at": self._clock(),
                    "victim": record.task_id,
                    "victim_kind": record.request.kind,
                    "victim_tenant": record.request.tenant,
                    "requester": requester,
                    "reason": reason,
                    "rounds": rounds_of(record),
                    "status": ack.status.value,
                    "checkpoint": ack.checkpoint,
                }
            )

    def preemption_log(self) -> list[dict[str, Any]]:
        """抢占台账快照（有界，最多 100 条）。没有抢占就返回空列表。"""
        with self._lock:
            return list(self._preempt_log)

    def resume(self, task_id: str) -> TaskRecord:
        """从安全点续跑。没有状态就不许叫"续跑"——那只能重新提交。"""
        record = self.get(task_id)
        with self._lock:
            if record.status is not TaskStatus.PREEMPTED:
                raise conflict(
                    "只有停在安全点（preempted）的任务能恢复",
                    task_id=task_id,
                    status=record.status.value,
                )
            if not record.preempt or "state" not in record.preempt:
                raise conflict(
                    "没有可恢复的安全点状态，不能续跑；请重新提交",
                    task_id=task_id,
                    hint="执行器没走到安全点就停了，此时无从恢复",
                )
            record.preempt_requested = False
            self._cancel_events.setdefault(task_id, threading.Event())
            self._preempt_events[task_id] = threading.Event()
            request = record.request
            record.transition_to(TaskStatus.QUEUED, reason="resume", clock=self._clock)
            self._queued_total += 1
            self._queued_by_kind[request.kind] = self._queued_by_kind.get(request.kind, 0) + 1
            self._queued_by_tenant[request.tenant] = (
                self._queued_by_tenant.get(request.tenant, 0) + 1
            )
            self._queue.push(record)

        self._metrics.counter("tasks.resumed").inc()
        self._metrics.gauge("queue.depth").set(float(self._queued_total))
        log_event(self._logger, "task.resumed", task_id=task_id, status=record.status.value)
        return record

    def preempted(self) -> list[TaskRecord]:
        """停在安全点上、等恢复的任务。"""
        return [record for record in self.list_tasks() if record.status is TaskStatus.PREEMPTED]

    def mark_for_recovery(self, task_id: str) -> TaskRecord:
        """把非终态任务显式标记为「需要恢复」，不许留在 RUNNING 里装作还在跑。"""
        record = self.get(task_id)
        if record.is_terminal:
            return record
        with self._lock:
            if record.status in (
                TaskStatus.RUNNING,
                TaskStatus.CANCELLING,
                TaskStatus.PREEMPTING,
            ):
                record.transition_to(TaskStatus.RECLAIMING, reason="recovery", clock=self._clock)
        return record

    def reclaim(self, task_id: str) -> TaskRecord:
        """回收资源并进入 RECLAIMED 终态。"""
        record = self.get(task_id)
        with self._lock:
            if record.status is TaskStatus.RECLAIMING:
                self._release_lease(record)
                record.transition_to(TaskStatus.RECLAIMED, reason="reclaim", clock=self._clock)
        return record

    # ------------------------------------------------------------------ 内部

    def _loop(self) -> None:
        poll = self._config.runtime.worker_poll_timeout_s
        while True:
            record = self._queue.pop(timeout=poll)
            if record is None:
                if self._stopping.is_set():
                    return
                continue
            self._run(record)

    def _run(self, record: TaskRecord) -> None:
        with self._lock:
            self._queued_total = max(0, self._queued_total - 1)
            kind = record.request.kind
            tenant = record.request.tenant
            self._queued_by_kind[kind] = max(0, self._queued_by_kind.get(kind, 0) - 1)
            self._queued_by_tenant[tenant] = max(0, self._queued_by_tenant.get(tenant, 0) - 1)
            self._inflight += 1
            self._metrics.gauge("queue.depth").set(float(self._queued_total))

        if record.is_terminal:  # 提交后立刻被取消
            with self._lock:
                self._inflight -= 1
            return

        lease = None
        kv_tokens = 0
        try:
            with self._lock:
                record.attempts += 1
                record.transition_to(TaskStatus.RUNNING, reason="dispatch", clock=self._clock)
            lease = self._acquire_lease(record)
            with self._lock:
                record.lease_id = lease.lease_id

            # KV 调度许可（WBS-3.5）：预算不够就在开跑前拒绝，不许带着"也许够"的预算执行
            kv_tokens = self._reserve_kv(record)
            context = ExecutionContext(
                task_id=record.task_id,
                budget=record.request.budget,
                cancel_event=self._cancel_events.get(record.task_id) or threading.Event(),
                config=self._config,
                preempt_event=self._preempt_events.get(record.task_id) or threading.Event(),
                resume_state=(record.preempt or {}).get("state"),
            )
            result = self._executor.execute(context, record.request)
            with self._lock:
                record.result = result
                target = (
                    TaskStatus.CANCELLED
                    if record.status is TaskStatus.CANCELLING
                    else TaskStatus.SUCCEEDED
                )
                record.transition_to(target, reason="executor.returned", clock=self._clock)
            self._metrics.counter("tasks.succeeded").inc()
            self._metrics.samples("task.elapsed_ms").observe(
                float(result.metrics.get("elapsed_ms", 0.0))
            )
        except TaskPreempted as exc:
            # 必须排在 LiteInferError 前面：抢占不是失败，是"停在安全点等恢复"
            self._finish_preempted(record, exc, lease)
        except TaskCancelled as exc:
            self._finish_failed(record, exc, status=TaskStatus.CANCELLED)
            self._metrics.counter("tasks.cancelled").inc()
        except LiteInferError as exc:
            # 领域错误必须保留原码，不能被降级成 internal
            self._finish_failed(record, exc, status=TaskStatus.FAILED)
            self._metrics.counter("tasks.failed").inc()
        except Exception as exc:  # noqa: BLE001 - 任何异常都必须落到终态，不允许悬空
            self._finish_failed(record, exc, status=TaskStatus.FAILED)
            self._metrics.counter("tasks.failed").inc()
        finally:
            # lease_id 为空说明已经释放过（抢占路径会先还），不许重复释放把证据覆盖成失败
            if (
                lease is not None
                and self._config.runtime.reclaim_on_terminal
                and record.lease_id is not None
            ):
                self._release_lease(record)
            if kv_tokens:
                self._kv_cache.release(kv_tokens, owner=record.task_id)
            with self._lock:
                self._inflight -= 1
                self._metrics.gauge("inflight").set(float(self._inflight))
                self._cancel_events.pop(record.task_id, None)
            # 资源已经还了，才轮到把它抢过的任务唤醒（顺序反了就是又一次抢不到）
            self._wake_victims_of(record.task_id)
            log_event(
                self._logger,
                "task.finished",
                task_id=record.task_id,
                status=record.status.value,
            )

    def _reserve_kv(self, record: TaskRecord) -> int:
        """申请 KV 缓存调度许可。声明了 `kv_tokens` 才申请；没声明就不占位。"""
        declared = record.request.payload.get("kv_tokens", 0)
        if not isinstance(declared, int) or isinstance(declared, bool):
            raise invalid_argument("kv_tokens 必须是整数", task_id=record.task_id)
        if declared <= 0:
            return 0
        self._kv_cache.reserve(declared, owner=record.task_id)
        return declared

    def kv_cache(self) -> KVCacheBudget:
        return self._kv_cache

    def tensor_cache(self) -> TensorFeatureCache:
        return self._tensor_cache

    def cache_stats(self) -> dict[str, Any]:
        """两类缓存各自独立的指标（基线 §6.2：预算分开、指标分开）。"""
        return {
            "kv": self._kv_cache.stats().to_dict(),
            "tensor": self._tensor_cache.stats().to_dict(),
        }

    def _acquire_lease(self, record: TaskRecord) -> ResourceLease:
        """取租约；资源不足且开着自动抢占时，先按策略抢让路任务。

        **只在资源不足（resource_exhausted）时触发**：别的错误（非法配置等）
        必须原样抛出，不许被抢占逻辑吃掉。
        """
        try:
            return self._lease_manager.acquire(record.task_id, record.request.budget)
        except LiteInferError as exc:
            policy = self._config.preempt
            if not (policy.enabled and policy.auto):
                raise
            if exc.code is not ErrorCode.RESOURCE_EXHAUSTED:
                raise
            return self._preempt_and_retry(record, exc)

    def _preempt_and_retry(self, record: TaskRecord, original: LiteInferError) -> ResourceLease:
        """逐个让候选 victim 到安全点，每让一个就重试一次取租约。"""
        policy = self._config.preempt
        for candidate in self._preemption.candidates(
            record.request, self.list_tasks(status=TaskStatus.RUNNING)
        ):
            if candidate.task_id == record.task_id:
                continue  # 不许自己抢自己
            try:
                self.preempt(
                    candidate.task_id,
                    timeout_s=policy.wait_timeout_s,
                    requester=record.task_id,
                    reason=f"auto:{candidate.reason}",
                )
            except LiteInferError:
                continue  # 这个让不了（可能在等待期间已经跑完），换下一个候选
            try:
                return self._lease_manager.acquire(record.task_id, record.request.budget)
            except LiteInferError:
                continue
        raise original

    def _wake_victims_of(self, task_id: str) -> None:
        """谁抢的谁还：请求者跑完释放资源后，唤醒被它抢到安全点的任务。

        没有这一步，自动抢占等于把低优先级任务永久挂起——那不是 QoS，那是丢任务。
        只恢复**自己抢过的**那些，避免变成无法解释的全队列扫描。
        """
        policy = self._config.preempt
        if not (policy.enabled and policy.auto):
            return
        victims = [
            entry["victim"]
            for entry in self.preemption_log()
            if entry["requester"] == task_id and entry.get("checkpoint") is True
        ]
        for victim_id in victims:
            record = self.get(victim_id)
            if record.status is not TaskStatus.PREEMPTED:
                continue
            try:
                self.resume(victim_id)
            except LiteInferError:  # noqa: BLE001 - 唤醒失败要留痕，不能拖垮收尾流程
                log_event(
                    self._logger,
                    "preempt.resume_failed",
                    task_id=victim_id,
                    requester=task_id,
                )
                continue
            self._metrics.counter("tasks.auto_resumed").inc()
            log_event(self._logger, "preempt.auto_resumed", task_id=victim_id, requester=task_id)

    def _finish_preempted(
        self, record: TaskRecord, exc: TaskPreempted, lease: object | None
    ) -> None:
        """停在安全点：先存状态、再还资源、最后才进 PREEMPTED。"""
        with self._lock:
            previous = record.preempt or {}
            record.preempt = {
                "state": dict(exc.state),
                "at": self._clock(),
                "rounds": int(previous.get("rounds", 0)) + 1,
            }
        if lease is not None:
            # 抢占的意义就是腾资源，不等终态
            self._release_lease(record)
        with self._lock:
            if record.status in (TaskStatus.RUNNING, TaskStatus.PREEMPTING):
                record.transition_to(
                    TaskStatus.PREEMPTED, reason="preempt:safepoint", clock=self._clock
                )
        self._metrics.counter("tasks.preempted").inc()
        log_event(
            self._logger,
            "task.preempted",
            task_id=record.task_id,
            rounds=record.preempt["rounds"],
        )

    def _finish_failed(
        self, record: TaskRecord, exc: BaseException, *, status: TaskStatus
    ) -> None:
        error = (
            TaskError.from_exception(exc)
            if isinstance(exc, LiteInferError)
            else TaskError.from_unexpected(exc)
        )
        with self._lock:
            record.error = error
            if not record.is_terminal:
                record.transition_to(status, reason="executor.failed", clock=self._clock)

    def _release_lease(self, record: TaskRecord) -> None:
        lease_id = record.lease_id
        if lease_id is None:
            # 没有租约也要留痕：回收记录不能凭空消失
            record.reclaim = {"released": True, "lease_id": None, "note": "no_lease"}
            return
        try:
            sample = self._lease_manager.release(lease_id)
        except Exception as exc:  # noqa: BLE001 - 释放失败必须留痕，不能吞掉
            record.reclaim = {"released": False, "error": f"{type(exc).__name__}: {exc}"}
            return
        record.reclaim = {
            "released": True,
            "lease_id": lease_id,
            "released_sample": sample.to_dict(),
        }
        record.lease_id = None



