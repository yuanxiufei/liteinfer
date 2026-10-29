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
class PreemptionConfig:
    """安全抢占策略（WBS-3.9 / G2）。

    开放的门槛不是「执行器说自己行」，而是 G2 的原话：
    **只对已实现并通过恢复测试的任务类型开放**。因此：

    - `verified_kinds` 是准入台账：登记了才可能被抢占；
    - `verification` 是台账凭据：登记时必须写出让它获得资格的**恢复测试**，
      空口声称"支持抢占"不算通过；登记了却拿不出凭据属于配置错误；
    - 两者默认都为空 ⇒ **默认不对任何任务类型开放**，沿用既有口径。

    `max_rounds` 防止同一个任务被反复抢占而永远跑不完（抢占本身也会制造饥饿）；
    `auto` 允许资源不足时按优先级主动挑 victim，但要求至少两个 worker——
    单 worker 时 victim 就在等资源的那个线程上，等它到安全点是死锁。
    """

    enabled: bool = False
    #: 已实现并通过恢复测试、允许被抢占的 kind
    verified_kinds: tuple[str, ...] = ()
    #: kind → 恢复测试证据（测试 id / 命令），必须与 `verified_kinds` 一一对应
    verification: dict[str, str] = field(default_factory=dict)
    #: 同一任务最多被抢占几次；用尽即转为不可抢占
    max_rounds: int = 3
    #: 自动抢占时，请求者优先级至少高出 victim 多少
    min_priority_gap: int = 1
    auto: bool = False
    wait_timeout_s: float = 5.0

    def problems(self) -> list[str]:
        issues: list[str] = []
        if self.max_rounds < 1:
            issues.append(f"max_rounds 必须 >= 1，当前 {self.max_rounds}")
        if self.min_priority_gap < 0:
            issues.append(f"min_priority_gap 不能为负，当前 {self.min_priority_gap}")
        if self.wait_timeout_s <= 0:
            issues.append(f"wait_timeout_s 必须 > 0，当前 {self.wait_timeout_s}")
        for kind in self.verified_kinds:
            if not kind.strip():
                issues.append("verified_kinds 里不允许出现空字符串")
                continue
            if not (self.verification.get(kind) or "").strip():
                issues.append(
                    f"verified_kinds 里的 {kind} 没有取证依据："
                    f"必须在 verification 中登记它通过了哪项恢复测试"
                )
        for kind in self.verification:
            if kind not in self.verified_kinds:
                issues.append(
                    f"verification 里的 {kind} 不在 verified_kinds 中，"
                    f"拒绝挂空凭据（要么登记此类型，要么删掉这条证据）"
                )
        return issues

    def eligible(self, kind: str) -> bool:
        """该 kind 是否已登记为「实现并通过恢复测试」且抢占总开关已开。"""
        return self.enabled and kind in self.verified_kinds

    def evidence_of(self, kind: str) -> str:
        """返回该 kind 的恢复测试证据；没有就是空串，不许拿别的类型顶替。"""
        return self.verification.get(kind) or ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "verified_kinds": list(self.verified_kinds),
            "verification": dict(self.verification),
            "max_rounds": self.max_rounds,
            "min_priority_gap": self.min_priority_gap,
            "auto": self.auto,
            "wait_timeout_s": self.wait_timeout_s,
        }


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
    preempt: PreemptionConfig = field(default_factory=PreemptionConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)

    def problems(self) -> list[str]:
        issues: list[str] = []
        if not self.service_name.strip():
            issues.append("service_name 不能为空")
        issues.extend(self.queue.problems())
        issues.extend(self.quota.problems())
        issues.extend(self.cache.problems())
        issues.extend(self.preempt.problems())
        issues.extend(self.runtime.problems())
        issues.extend(self.observability.problems())
        issues.extend(self._cross_section_problems())
        return issues

    def _cross_section_problems(self) -> list[str]:
        """跨配置段的组合校验：单段都合法、拼起来会出事的，必须在这里拦住。"""
        issues: list[str] = []
        if self.preempt.enabled and self.preempt.auto and self.queue.max_inflight < 2:
            issues.append(
                "preempt.auto 要求 queue.max_inflight >= 2，当前 "
                f"{self.queue.max_inflight}：单 worker 时 victim 就在等待资源的那个线程上，"
                f"等它走到安全点必然死锁"
            )
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
            "preempt": self.preempt.to_dict(),
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
    preempt_raw = data.get("preempt") or {}
    runtime_raw = data.get("runtime") or {}
    observability_raw = data.get("observability") or {}
    for name, raw in (
        ("queue", queue_raw),
        ("quota", quota_raw),
        ("cache", cache_raw),
        ("preempt", preempt_raw),
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
        preempt=_build(PreemptionConfig, preempt_raw, section="preempt"),
        runtime=_build(RuntimeConfig, runtime_raw, section="runtime"),
        observability=_build(ObservabilityConfig, observability_raw, section="observability"),
    )
    config.validate()
    return config
