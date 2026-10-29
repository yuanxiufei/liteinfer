"""配置模型与校验（WBS-1.7）。

纪律：配置错误必须在启动或提交前报出可读错误，不允许带着非法值继续跑。
`from_mapping` 对未知键一律拒绝——被静默忽略的配置比报错更危险。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Any, TypeVar

from .errors import invalid_argument

_T = TypeVar("_T")


@dataclass(frozen=True)
class QueueConfig:
    """队列上限（WBS-3.1 的最小形态）。

    排队上限只约束「未开始执行」的任务；运行中的任务数由并发上限约束。
    """

    max_queued: int = 1024
    max_queued_per_kind: int = 256
    max_queued_per_tenant: int = 256
    max_inflight: int = 1

    def problems(self) -> list[str]:
        issues: list[str] = []
        for name in ("max_queued", "max_queued_per_kind", "max_queued_per_tenant", "max_inflight"):
            value = getattr(self, name)
            if value < 1:
                issues.append(f"{name} 必须 >= 1，当前 {value}")
        return issues


@dataclass(frozen=True)
class RuntimeConfig:
    """执行侧行为。真实显存计量接入前，租约默认按声明预算记账。"""

    reclaim_on_terminal: bool = True
    executor_timeout_s: float | None = None
    worker_poll_timeout_s: float = 0.2

    def problems(self) -> list[str]:
        issues: list[str] = []
        if self.executor_timeout_s is not None and self.executor_timeout_s <= 0:
            issues.append(f"executor_timeout_s 必须 > 0，当前 {self.executor_timeout_s}")
        if self.worker_poll_timeout_s <= 0:
            issues.append(f"worker_poll_timeout_s 必须 > 0，当前 {self.worker_poll_timeout_s}")
        return issues


@dataclass(frozen=True)
class ObservabilityConfig:
    level: str = "INFO"
    json_logs: bool = True
    log_file: str | None = None

    def problems(self) -> list[str]:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if self.level.upper() not in allowed:
            return [f"level 必须是 {sorted(allowed)} 之一，当前 {self.level}"]
        return []


@dataclass(frozen=True)
class LiteInferConfig:
    service_name: str = "liteinfer"
    queue: QueueConfig = field(default_factory=QueueConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)

    def problems(self) -> list[str]:
        issues: list[str] = []
        if not self.service_name.strip():
            issues.append("service_name 不能为空")
        issues.extend(self.queue.problems())
        issues.extend(self.runtime.problems())
        issues.extend(self.observability.problems())
        return issues

    def validate(self) -> None:
        issues = self.problems()
        if issues:
            raise invalid_argument("配置校验失败", issues=issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_name": self.service_name,
            "queue": _as_dict(self.queue),
            "runtime": _as_dict(self.runtime),
            "observability": _as_dict(self.observability),
        }


def _as_dict(obj: Any) -> dict[str, Any]:
    return {f.name: getattr(obj, f.name) for f in fields(obj)}


def _build(cls: type[_T], raw: Mapping[str, Any], *, section: str) -> _T:
    allowed = {f.name for f in fields(cls)}  # type: ignore[arg-type]
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise invalid_argument(f"{section} 含未知配置项", section=section, unknown=unknown)
    try:
        return cls(**raw)
    except TypeError as exc:
        raise invalid_argument(f"{section} 配置类型错误：{exc}", section=section) from exc


def config_from_mapping(data: Mapping[str, Any]) -> LiteInferConfig:
    """从字典构造配置。缺项用默认值，未知项与类型错误直接报错。"""
    allowed = {f.name for f in fields(LiteInferConfig)}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise invalid_argument("配置含未知段", unknown=unknown)

    queue_raw = data.get("queue") or {}
    runtime_raw = data.get("runtime") or {}
    observability_raw = data.get("observability") or {}
    for name, raw in (
        ("queue", queue_raw),
        ("runtime", runtime_raw),
        ("observability", observability_raw),
    ):
        if not isinstance(raw, Mapping):
            raise invalid_argument(f"{name} 必须是对象", section=name)

    config = LiteInferConfig(
        service_name=str(data.get("service_name") or LiteInferConfig.service_name),
        queue=_build(QueueConfig, queue_raw, section="queue"),
        runtime=_build(RuntimeConfig, runtime_raw, section="runtime"),
        observability=_build(ObservabilityConfig, observability_raw, section="observability"),
    )
    config.validate()
    return config
