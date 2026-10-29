"""资源租约与计量（WBS-1 / WBS-3）。

**关键边界**：这是「逻辑配额 + 记账」，不是 GPU 显存硬隔离器。
若底层组件绕过 LiteInfer 的分配接口直接申请显存，本模块只能记账、
告警和拒绝新租约，不能强制回收别人的显存。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from ..core import ResourceBudget, new_lease_id, not_found, resource_exhausted


@dataclass(frozen=True)
class ResourceSample:
    """一次资源采样。三类资源分别记录，禁止相加成一个总数。"""

    vram_bytes: int = 0
    ram_bytes: int = 0
    ssd_bytes: int = 0
    at: float = field(default_factory=time.time)
    source: str = "declared"

    def to_dict(self) -> dict[str, object]:
        return {
            "vram_bytes": self.vram_bytes,
            "ram_bytes": self.ram_bytes,
            "ssd_bytes": self.ssd_bytes,
            "at": self.at,
            "source": self.source,
        }


class ResourceMeter(Protocol):
    """资源采样接口。NVML / torch / 系统接口的实现在 WBS-3 接入。"""

    def sample(self) -> ResourceSample: ...


class CallbackMeter:
    """用回调函数构造的 meter，便于测试与后续接入真实采样。"""

    def __init__(self, sampler: Callable[[], ResourceSample], *, source: str = "callback") -> None:
        self._sampler = sampler
        self._source = source

    def sample(self) -> ResourceSample:
        sample = self._sampler()
        return ResourceSample(
            vram_bytes=sample.vram_bytes,
            ram_bytes=sample.ram_bytes,
            ssd_bytes=sample.ssd_bytes,
            at=time.time(),
            source=self._source,
        )


@dataclass
class ResourceLease:
    lease_id: str
    task_id: str
    budget: ResourceBudget
    acquired_at: float
    acquired_sample: ResourceSample
    released_at: float | None = None
    released_sample: ResourceSample | None = None

    @property
    def active(self) -> bool:
        return self.released_at is None

    def to_dict(self) -> dict[str, object]:
        return {
            "lease_id": self.lease_id,
            "task_id": self.task_id,
            "budget": self.budget.to_dict(),
            "acquired_at": self.acquired_at,
            "acquired_sample": self.acquired_sample.to_dict(),
            "released_at": self.released_at,
            "released_sample": self.released_sample.to_dict() if self.released_sample else None,
            "active": self.active,
        }


class LeaseManager:
    """按声明预算记账的租约管理器。

    `capacity` 全零表示不设上限（真实上限要等 WBS-3 的实测配额）。
    """

    def __init__(
        self,
        meter: ResourceMeter | None = None,
        *,
        capacity: ResourceBudget | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._meter = meter
        self._capacity = capacity or ResourceBudget()
        self._clock = clock
        self._lock = threading.RLock()
        self._leases: dict[str, ResourceLease] = {}
        self._peak = ResourceBudget()

    @property
    def capacity(self) -> ResourceBudget:
        return self._capacity

    def acquire(self, task_id: str, budget: ResourceBudget) -> ResourceLease:
        with self._lock:
            outstanding = self.outstanding()
            if self._exceeds(outstanding, budget):
                raise resource_exhausted(
                    "资源预算不足，拒绝新建租约",
                    task_id=task_id,
                    requested=budget.to_dict(),
                    outstanding=outstanding.to_dict(),
                    capacity=self._capacity.to_dict(),
                )
            sample = self._meter.sample() if self._meter else ResourceSample(source="declared")
            lease = ResourceLease(
                lease_id=new_lease_id(),
                task_id=task_id,
                budget=budget,
                acquired_at=self._clock(),
                acquired_sample=sample,
            )
            self._leases[lease.lease_id] = lease
            self._update_peak(outstanding, budget)
            return lease

    def release(self, lease_id: str) -> ResourceSample:
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                raise not_found("租约不存在或已释放", lease_id=lease_id)
            if lease.released_at is not None:
                raise not_found("租约已释放，不能重复释放", lease_id=lease_id)
            sample = self._meter.sample() if self._meter else ResourceSample(source="declared")
            lease.released_at = self._clock()
            lease.released_sample = sample
            del self._leases[lease_id]
            return sample

    def active(self) -> list[ResourceLease]:
        with self._lock:
            return [lease for lease in self._leases.values() if lease.active]

    def outstanding(self) -> ResourceBudget:
        with self._lock:
            vram = ram = ssd = 0
            for lease in self._leases.values():
                vram += lease.budget.vram_bytes
                ram += lease.budget.ram_bytes
                ssd += lease.budget.ssd_bytes
            return ResourceBudget(vram_bytes=vram, ram_bytes=ram, ssd_bytes=ssd)

    def peak(self) -> ResourceBudget:
        with self._lock:
            return self._peak

    def assert_no_leak(self) -> None:
        """泄漏断言：仍有未释放租约即报错。给测试与关闭流程用。"""
        active = self.active()
        if active:
            raise resource_exhausted(
                "存在未释放的资源租约",
                lease_ids=[lease.lease_id for lease in active],
                task_ids=[lease.task_id for lease in active],
            )

    def _exceeds(self, outstanding: ResourceBudget, extra: ResourceBudget) -> bool:
        capacity = self._capacity
        for kind in ("vram_bytes", "ram_bytes", "ssd_bytes"):
            limit = getattr(capacity, kind)
            if limit <= 0:
                continue  # 0 表示该项不设上限
            if getattr(outstanding, kind) + getattr(extra, kind) > limit:
                return True
        return False

    def _update_peak(self, outstanding: ResourceBudget, extra: ResourceBudget) -> None:
        self._peak = ResourceBudget(
            vram_bytes=max(self._peak.vram_bytes, outstanding.vram_bytes + extra.vram_bytes),
            ram_bytes=max(self._peak.ram_bytes, outstanding.ram_bytes + extra.ram_bytes),
            ssd_bytes=max(self._peak.ssd_bytes, outstanding.ssd_bytes + extra.ssd_bytes),
        )
