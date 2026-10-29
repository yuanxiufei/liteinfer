"""核心领域模型（WBS-1.2）。

纪律：本模块只描述领域事实，不依赖 SGLang、PyTorch 模型类或 Web 框架。
权重 / 适配器只登记引用信息，真实加载由 WBS-2 的适配器层负责。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar

from .errors import LiteInferError, invalid_argument
from .ids import new_task_id
from .state import TaskStatus, ensure_transition, is_terminal

_T = TypeVar("_T")


@dataclass(frozen=True)
class ModelRef:
    """模型引用。版本必须显式登记，不允许用「最新」二字代替。"""

    model_id: str
    version: str = "unpinned"
    kind: str = "unknown"
    revision: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "version": self.version,
            "kind": self.kind,
            "revision": self.revision,
        }


@dataclass(frozen=True)
class WeightRef:
    """权重文件引用（WBS-2.6 的清单条目形状）。"""

    source: str
    format: str = "unknown"
    sha256: str | None = None
    size_bytes: int | None = None
    license: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "format": self.format,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "license": self.license,
        }


@dataclass(frozen=True)
class AdapterRef:
    adapter_id: str
    api_version: str = "1.0.0"
    base_model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "api_version": self.api_version,
            "base_model": self.base_model,
        }


@dataclass(frozen=True)
class ResourceBudget:
    """三类资源分别计量，禁止合并成一个「总内存」。"""

    vram_bytes: int = 0
    ram_bytes: int = 0
    ssd_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "vram_bytes": self.vram_bytes,
            "ram_bytes": self.ram_bytes,
            "ssd_bytes": self.ssd_bytes,
        }


@dataclass(frozen=True)
class TaskRequest:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    model: ModelRef | None = None
    budget: ResourceBudget = field(default_factory=ResourceBudget)
    priority: int = 0
    tenant: str = "default"
    client_token: str | None = None
    labels: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "payload": self.payload,
            "model": self.model.to_dict() if self.model else None,
            "budget": self.budget.to_dict(),
            "priority": self.priority,
            "tenant": self.tenant,
            "client_token": self.client_token,
            "labels": self.labels,
        }


@dataclass(frozen=True)
class TaskResult:
    output: Any = None
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"output": self.output, "metrics": self.metrics}


@dataclass(frozen=True)
class TaskError:
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    retryable: bool = False

    @classmethod
    def from_exception(cls, exc: LiteInferError) -> TaskError:
        return cls(
            code=exc.code.value,
            message=exc.message,
            details=dict(exc.details),
            retryable=exc.retryable,
        )

    @classmethod
    def from_unexpected(cls, exc: BaseException) -> TaskError:
        return cls(code="internal", message=f"{type(exc).__name__}: {exc}", retryable=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "retryable": self.retryable,
        }


@dataclass(frozen=True)
class StateTransition:
    source: TaskStatus
    target: TaskStatus
    at: float
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.value,
            "target": self.target.value,
            "at": self.at,
            "reason": self.reason,
        }


@dataclass
class TaskRecord:
    """任务的唯一权威记录。状态只能通过 `transition_to` 变更。"""

    request: TaskRequest
    task_id: str = field(default_factory=new_task_id)
    status: TaskStatus = TaskStatus.SUBMITTED
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    result: TaskResult | None = None
    error: TaskError | None = None
    lease_id: str | None = None
    reclaim: dict[str, Any] | None = None
    #: 抢占落点（WBS-3.9）：执行器在安全点交出的状态 + 停下时刻 + 已抢占次数。
    #: 没有它就谈不上"续跑"，只能重新提交。
    preempt: dict[str, Any] | None = None
    attempts: int = 0
    cancel_requested: bool = False
    preempt_requested: bool = False
    history: list[StateTransition] = field(default_factory=list)
    _done: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

    def transition_to(
        self,
        target: TaskStatus,
        *,
        reason: str = "",
        clock: Callable[[], float] = time.time,
    ) -> StateTransition:
        ensure_transition(self.status, target, task_id=self.task_id)
        now = clock()
        transition = StateTransition(source=self.status, target=target, at=now, reason=reason)
        self.status = target
        self.updated_at = now
        self.history.append(transition)
        if target == TaskStatus.RUNNING:
            self.started_at = now
        if is_terminal(target):
            self.finished_at = now
            self._done.set()
        return transition

    @property
    def done(self) -> threading.Event:
        return self._done

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self.status)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "request": self.request.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result.to_dict() if self.result else None,
            "error": self.error.to_dict() if self.error else None,
            "lease_id": self.lease_id,
            "reclaim": self.reclaim,
            "preempt": self.preempt,
            "attempts": self.attempts,
            "cancel_requested": self.cancel_requested,
            "needs_recovery": not self.is_terminal,
            "history": [item.to_dict() for item in self.history],
        }


_ALLOWED_REQUEST_FIELDS = frozenset(
    {"kind", "payload", "model", "budget", "priority", "tenant", "client_token", "labels"}
)


def _build(cls: type[_T], raw: Mapping[str, Any], *, field_name: str) -> _T:
    """按白名单构造 dataclass；未知字段拒绝，避免配置项被静默忽略。"""
    fields_meta = getattr(cls, "__dataclass_fields__", {})
    allowed = {name for name in fields_meta if not name.startswith("_")}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise invalid_argument(f"{field_name} 含未知字段", field=field_name, unknown=unknown)
    return cls(**raw)


def request_from_mapping(data: Mapping[str, Any]) -> TaskRequest:
    """从外部输入构造请求。类型错误与未知字段都报可读错误，不静默吞参数。"""
    unknown = sorted(set(data) - _ALLOWED_REQUEST_FIELDS)
    if unknown:
        raise invalid_argument("请求含未知字段", unknown=unknown)
    if not str(data.get("kind") or "").strip():
        raise invalid_argument("kind 不能为空")

    model_raw = data.get("model")
    model = _build(ModelRef, model_raw, field_name="model") if model_raw is not None else None
    budget_raw = data.get("budget")
    budget = (
        _build(ResourceBudget, budget_raw, field_name="budget")
        if budget_raw is not None
        else ResourceBudget()
    )
    payload = data.get("payload") or {}
    if not isinstance(payload, Mapping):
        raise invalid_argument("payload 必须是对象")

    try:
        priority = int(data.get("priority", 0))
    except (TypeError, ValueError) as exc:
        raise invalid_argument("priority 必须是整数") from exc

    return TaskRequest(
        kind=str(data["kind"]),
        payload=dict(payload),
        model=model,
        budget=budget,
        priority=priority,
        tenant=str(data.get("tenant") or "default"),
        client_token=data.get("client_token"),
        labels=dict(data.get("labels") or {}),
    )
