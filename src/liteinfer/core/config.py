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
    """队列上限与调度（WBS-3.1 / 3.10）。

    排队上限只约束「未开始执行」的任务；运行中的任务数由 `max_inflight` 约束
    （它就是 worker 数，不是只计数不生效的装饰）。
    """

    max_queued: int = 1024
    max_queued_per_kind: int = 256
    max_queued_per_tenant: int = 256
    #: 并发上限 = worker 线程数。曾一度只被计数、从不限制，那是缺陷不是设计。
    max_inflight: int = 1
    #: 同一 tenant 连续出队多少个后让位给别人（0 = 不启用公平性限制）
    fairness_limit: int = 0
    #: kind → 类别（inference / eval / training）的显式映射，覆盖前缀规则
    kind_class: dict[str, str] = field(default_factory=dict)

    def problems(self) -> list[str]:
        issues: list[str] = []
        for name in ("max_queued", "max_queued_per_kind", "max_queued_per_tenant", "max_inflight"):
            value = getattr(self, name)
            if value < 1:
                issues.append(f"{name} 必须 >= 1，当前 {value}")
        if self.fairness_limit < 0:
            issues.append(f"fairness_limit 不能为负，当前 {self.fairness_limit}")
        allowed = {"inference", "eval", "training"}
        for kind, name in self.kind_class.items():
            if name not in allowed:
                issues.append(f"kind_class[{kind}] 必须是 {sorted(allowed)} 之一，当前 {name}")
        return issues


@dataclass(frozen=True)
class QuotaConfig:
    """静态资源配额（WBS-3.3）。

    `0` 一律表示**不设限**，不是"限额为零"：真实配额要等 WBS-0.2 锁定并在 GPU 上实测，
    未实测前不得填假数字。`reserved_*` 是不可动用的保留空间，从可用容量里扣掉。
    """

    vram_bytes: int = 0
    ram_bytes: int = 0
    ssd_bytes: int = 0
    reserved_vram_bytes: int = 0
    reserved_ram_bytes: int = 0
    reserved_ssd_bytes: int = 0
    #: 弹性借用上限（WBS-3.8）：只借基础配额之外的部分，且必须记录借用者
    borrow_vram_bytes: int = 0
    borrow_ram_bytes: int = 0
    borrow_ssd_bytes: int = 0

    def problems(self) -> list[str]:
        issues: list[str] = []
        for name in (
            "vram_bytes",
            "ram_bytes",
            "ssd_bytes",
            "reserved_vram_bytes",
            "reserved_ram_bytes",
            "reserved_ssd_bytes",
            "borrow_vram_bytes",
            "borrow_ram_bytes",
            "borrow_ssd_bytes",
        ):
            if getattr(self, name) < 0:
                issues.append(f"{name} 不能为负，当前 {getattr(self, name)}")
        for kind in ("vram", "ram", "ssd"):
            limit = getattr(self, f"{kind}_bytes")
            reserved = getattr(self, f"reserved_{kind}_bytes")
            if limit > 0 and reserved >= limit:
                issues.append(
                    f"reserved_{kind}_bytes（{reserved}）必须小于 {kind}_bytes（{limit}），"
                    f"否则没有可用容量"
                )
        return issues

    def usable(self) -> dict[str, int]:
        """扣掉保留空间后的可用容量；未设限的项返回 0（沿用「0 = 不设限」）。"""
        result: dict[str, int] = {}
        for kind in ("vram", "ram", "ssd"):
            limit = getattr(self, f"{kind}_bytes")
            reserved = getattr(self, f"reserved_{kind}_bytes")
            result[f"{kind}_bytes"] = 0 if limit <= 0 else max(0, limit - reserved)
        return result

    def borrow_limit(self) -> dict[str, int]:
        return {
            f"{kind}_bytes": getattr(self, f"borrow_{kind}_bytes")
            for kind in ("vram", "ram", "ssd")
        }


@dataclass(frozen=True)
class CacheConfig:
    """两类缓存的预算（WBS-3.5 / 3.6）。

    基线 §6.2：**两类缓存预算分开**。KV 按 token 记，张量特征按字节记，
    两者不可互换、不可合并成一个"总缓存"。
    `0` = 不设限。真实数字要来自实测，未实测前不得填假值。
    """

    kv_tokens: int = 0
    tensor_bytes: int = 0

    def problems(self) -> list[str]:
        issues: list[str] = []
        for name in ("kv_tokens", "tensor_bytes"):
            if getattr(self, name) < 0:
                issues.append(f"{name} 不能为负，当前 {getattr(self, name)}")
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
    quota: QuotaConfig = field(default_factory=QuotaConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)

    def problems(self) -> list[str]:
        issues: list[str] = []
        if not self.service_name.strip():
            issues.append("service_name 不能为空")
        issues.extend(self.queue.problems())
        issues.extend(self.quota.problems())
        issues.extend(self.cache.problems())
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
            "quota": _as_dict(self.quota),
            "cache": _as_dict(self.cache),
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
    quota_raw = data.get("quota") or {}
    cache_raw = data.get("cache") or {}
    runtime_raw = data.get("runtime") or {}
    observability_raw = data.get("observability") or {}
    for name, raw in (
        ("queue", queue_raw),
        ("quota", quota_raw),
        ("cache", cache_raw),
        ("runtime", runtime_raw),
        ("observability", observability_raw),
    ):
        if not isinstance(raw, Mapping):
            raise invalid_argument(f"{name} 必须是对象", section=name)

    config = LiteInferConfig(
        service_name=str(data.get("service_name") or LiteInferConfig.service_name),
        queue=_build(QueueConfig, queue_raw, section="queue"),
        quota=_build(QuotaConfig, quota_raw, section="quota"),
        cache=_build(CacheConfig, cache_raw, section="cache"),
        runtime=_build(RuntimeConfig, runtime_raw, section="runtime"),
        observability=_build(ObservabilityConfig, observability_raw, section="observability"),
    )
    config.validate()
    return config
