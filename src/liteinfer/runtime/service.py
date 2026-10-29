"""任务服务：生命周期、队列上限、背压、取消与回收（WBS-1.3 / WBS-3）。

口径：
1. 每个任务都有唯一 ID、明确终态、错误信息与资源回收记录；
2. 队列满时**快速拒绝**，不允许请求被悄悄塞进后台；
3. 客户端重试通过 `client_token` 幂等，不制造重复任务；
4. 取消区分「已接受取消」与「已完成取消」，且只对声明支持安全点的执行器开放。
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..core import (
    LiteInferConfig,
    LiteInferError,
    TaskCancelled,
    TaskRecord,
    TaskRequest,
    TaskStatus,
    conflict,
    dependency_missing,
    invalid_argument,
    not_found,
    queue_full,
    request_from_mapping,
    timed_out,
    unsupported,
)
from ..core.models import TaskError
from ..observability import MetricRegistry, get_logger, log_event
from .executor import ExecutionContext, Executor, default_executor
from .lease import LeaseManager
from .ports import AdapterInfo, AdapterLookup


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


class TaskService:
    """进程内任务服务。HTTP 层与 SDK 共用同一套实例语义。"""

    def __init__(
        self,
        config: LiteInferConfig | None = None,
        executor: Executor | None = None,
        *,
        lease_manager: LeaseManager | None = None,
        adapter_lookup: AdapterLookup | None = None,
        start_worker: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config or LiteInferConfig()
        self._config.validate()
        self._executor = executor or default_executor()
        self._lease_manager = lease_manager or LeaseManager()
        self._adapter_lookup = adapter_lookup
        self._clock = clock
        self._logger = get_logger("service")
        self._metrics = MetricRegistry()

        self._lock = threading.RLock()
        self._records: dict[str, TaskRecord] = {}
        self._tokens: dict[str, str] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._queue: queue.Queue[TaskRecord] = queue.Queue()
        self._queued_total = 0
        self._queued_by_kind: dict[str, int] = {}
        self._queued_by_tenant: dict[str, int] = {}
        self._inflight = 0
        self._closed = False

        self._worker: threading.Thread | None = None
        if start_worker:
            self.start()

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._closed = False
            self._worker = threading.Thread(
                target=self._loop, name="liteinfer-worker", daemon=True
            )
            self._worker.start()

    def close(self, *, timeout_s: float = 5.0, mark_unfinished: bool = True) -> None:
        """停止消费并把未终结任务标记为需要恢复（而不是悄悄丢掉）。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            worker = self._worker
        if worker is not None:
            self._queue.put(None)  # type: ignore[arg-type]
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
            self._queue.put(record)

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
            return {
                "queued": self._queued_total,
                "inflight": self._inflight,
                "by_kind": dict(self._queued_by_kind),
                "by_tenant": dict(self._queued_by_tenant),
            }

    def metrics(self) -> dict[str, Any]:
        return self._metrics.snapshot()

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

    def mark_for_recovery(self, task_id: str) -> TaskRecord:
        """把非终态任务显式标记为「需要恢复」，不许留在 RUNNING 里装作还在跑。"""
        record = self.get(task_id)
        if record.is_terminal:
            return record
        with self._lock:
            if record.status in (TaskStatus.RUNNING, TaskStatus.CANCELLING):
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
        while True:
            item = self._queue.get()
            if item is None:
                return
            try:
                self._run(item)
            finally:
                self._queue.task_done()

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
        try:
            with self._lock:
                record.attempts += 1
                record.transition_to(TaskStatus.RUNNING, reason="dispatch", clock=self._clock)
            lease = self._lease_manager.acquire(record.task_id, record.request.budget)
            with self._lock:
                record.lease_id = lease.lease_id

            context = ExecutionContext(
                task_id=record.task_id,
                budget=record.request.budget,
                cancel_event=self._cancel_events.get(record.task_id) or threading.Event(),
                config=self._config,
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
            if lease is not None and self._config.runtime.reclaim_on_terminal:
                self._release_lease(record)
            with self._lock:
                self._inflight -= 1
                self._metrics.gauge("inflight").set(float(self._inflight))
                self._cancel_events.pop(record.task_id, None)
            log_event(
                self._logger,
                "task.finished",
                task_id=record.task_id,
                status=record.status.value,
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



