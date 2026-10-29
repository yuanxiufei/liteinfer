"""两类缓存的预算、计量与淘汰（WBS-3.5 / 3.6）。

基线 §6.2 的实现边界（**不许越过**）：

- **KV 缓存**：预算、token 占用统计、命中/淘汰/回收计数、调度许可（`reserve`）。
  不做：直接管理后端内部缓存块。
- **张量特征缓存**：容量上限、LRU 淘汰、对象引用（pin/unpin）与生命周期。
  不做：把所有外部张量自动纳入统一内存池。
- **两类缓存隔离**：预算分开、指标分开、淘汰日志分开；超额时只拒绝或淘汰**自身**缓存。
  不做：物理显存硬隔离（做不到，也不许这样宣称）。
- **缓存回收**：释放未 pin 的对象，并回调 `on_evict` 通知调用方。
  不做：打断在途任务的强制回收、CUDA 工作同步。

占用值是**记账值**（调用方上报 size/tokens），不是实测值；
真实计量要等 WBS-3.4 的 NVML 采样。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..core import LiteInferError, invalid_argument, not_found, resource_exhausted

_EVICTION_LOG_LIMIT = 100


# --------------------------------------------------------------------- KV 缓存


@dataclass
class KVCacheStats:
    """KV 缓存账目。token 数与字节数不混算。"""

    capacity_tokens: int = 0
    used_tokens: int = 0
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    evicted_tokens: int = 0
    releases: int = 0
    recycled_tokens: int = 0
    rejects: int = 0
    peak_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class KVCacheBudget:
    """KV 缓存预算与调度许可（WBS-3.5）。

    `capacity_tokens=0` 表示不设限。调度许可由 `reserve()` 给出：
    预算不够就 `resource_exhausted`（503），**不会**让任务带着"也许够"的预算开跑。
    """

    def __init__(self, *, capacity_tokens: int = 0) -> None:
        if capacity_tokens < 0:
            raise invalid_argument("capacity_tokens 不能为负", capacity_tokens=capacity_tokens)
        self._capacity = capacity_tokens
        self._lock = threading.RLock()
        self._used = 0
        self._stats = KVCacheStats(capacity_tokens=capacity_tokens)
        self._owners: dict[str, int] = {}

    @property
    def capacity_tokens(self) -> int:
        return self._capacity

    def reserve(self, tokens: int, *, owner: str | None = None) -> bool:
        """申请调度许可。不够就拒绝，不排队、不猜测。"""
        if tokens <= 0:
            raise invalid_argument("reserve 的 token 数必须 > 0", tokens=tokens)
        with self._lock:
            if self._capacity > 0 and self._used + tokens > self._capacity:
                self._stats.rejects += 1
                raise resource_exhausted(
                    "KV 缓存预算不足，拒绝授予调度许可",
                    requested=tokens,
                    used=self._used,
                    capacity=self._capacity,
                    owner=owner,
                    hint="提高 kv_tokens 预算，或等待在途任务释放",
                )
            self._used += tokens
            self._stats.peak_tokens = max(self._stats.peak_tokens, self._used)
            if owner is not None:
                self._owners[owner] = self._owners.get(owner, 0) + tokens
            return True

    def release(self, tokens: int, *, owner: str | None = None) -> None:
        """归还预算。多还的部分按 0 截断，不允许把 used 搞成负数。"""
        if tokens <= 0:
            raise invalid_argument("release 的 token 数必须 > 0", tokens=tokens)
        with self._lock:
            if owner is not None:
                held = self._owners.get(owner, 0)
                given_back = min(tokens, held)
                self._owners[owner] = held - given_back
                if self._owners[owner] == 0:
                    del self._owners[owner]
            self._used = max(0, self._used - tokens)
            self._stats.releases += 1
            self._stats.recycled_tokens += tokens

    def evict(self, tokens: int) -> None:
        """主动淘汰（后端发起）。只减记账，不动后端真实块。"""
        if tokens <= 0:
            raise invalid_argument("evict 的 token 数必须 > 0", tokens=tokens)
        with self._lock:
            self._used = max(0, self._used - tokens)
            self._stats.evictions += 1
            self._stats.evicted_tokens += tokens

    def hit(self) -> None:
        with self._lock:
            self._stats.hits += 1

    def miss(self) -> None:
        with self._lock:
            self._stats.misses += 1

    def owners(self) -> dict[str, int]:
        with self._lock:
            return dict(self._owners)

    def stats(self) -> KVCacheStats:
        with self._lock:
            stats = KVCacheStats(**dict(self._stats.__dict__))
            stats.used_tokens = self._used
            return stats

    def assert_no_leak(self) -> None:
        with self._lock:
            if self._used:
                raise resource_exhausted(
                    "KV 缓存预算存在未归还部分",
                    used_tokens=self._used,
                    owners=dict(self._owners),
                )


# ------------------------------------------------------------- 张量特征缓存


@dataclass
class TensorCacheStats:
    """张量特征缓存账目。"""

    capacity_bytes: int = 0
    used_bytes: int = 0
    items: int = 0
    pinned: int = 0
    hits: int = 0
    misses: int = 0
    inserts: int = 0
    evictions: int = 0
    rejects: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class _Entry:
    value: Any
    size_bytes: int
    pinned: bool = False
    at: float = 0.0


class TensorFeatureCache:
    """带容量上限与 LRU 淘汰的张量特征缓存（WBS-3.6）。

    - 容量 `0` = 不设限。
    - 淘汰只淘汰**自己**的对象，且**跳过 pin 住的**（在途任务正在用）。
    - 淘汰通过 `on_evict(key, value, reason)` 回调通知调用方去释放框架对象；
      本模块不替调用方决定怎么释放，也不假装"删了 Python 对象就等于显存回落了"。
    - **不承诺物理显存硬隔离。**
    """

    def __init__(
        self,
        *,
        capacity_bytes: int = 0,
        on_evict: Callable[[str, Any, str], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if capacity_bytes < 0:
            raise invalid_argument("capacity_bytes 不能为负", capacity_bytes=capacity_bytes)
        self._capacity = capacity_bytes
        self._on_evict = on_evict
        self._clock = clock
        self._lock = threading.RLock()
        self._items: OrderedDict[str, _Entry] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._inserts = 0
        self._evictions = 0
        self._rejects = 0
        self._eviction_log: deque[dict[str, Any]] = deque(maxlen=_EVICTION_LOG_LIMIT)

    @property
    def capacity_bytes(self) -> int:
        return self._capacity

    @property
    def used_bytes(self) -> int:
        with self._lock:
            return sum(entry.size_bytes for entry in self._items.values())

    def put(self, key: str, value: Any, size_bytes: int) -> None:
        """放入缓存。装不下就淘汰自己的 LRU；仍然装不下就拒绝（不静默丢弃）。"""
        if not key:
            raise invalid_argument("缓存 key 不能为空")
        if size_bytes < 0:
            raise invalid_argument("size_bytes 不能为负", key=key, size_bytes=size_bytes)

        with self._lock:
            if self._capacity > 0 and size_bytes > self._capacity:
                self._rejects += 1
                raise resource_exhausted(
                    "单个对象超过张量缓存总容量，拒绝放入",
                    key=key,
                    size_bytes=size_bytes,
                    capacity=self._capacity,
                )

            existing = self._items.get(key)
            self._items[key] = _Entry(
                value, size_bytes, existing.pinned if existing else False, self._clock()
            )
            self._items.move_to_end(key)
            try:
                self._make_room(protect=key)
            except LiteInferError:
                # 装不下就回滚：不允许"放进去了一半"这种中间态
                if existing is None:
                    del self._items[key]
                else:
                    self._items[key] = existing
                    self._items.move_to_end(key)
                raise
            if existing is None:
                self._inserts += 1

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                self._misses += 1
                return None
            self._hits += 1
            self._items.move_to_end(key)
            return entry.value

    def pin(self, key: str) -> None:
        """pin 住的对象不参与淘汰（在途任务正在用）。"""
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                raise not_found("缓存对象不存在，不能 pin", key=key)
            entry.pinned = True

    def unpin(self, key: str) -> None:
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                raise not_found("缓存对象不存在，不能 unpin", key=key)
            entry.pinned = False

    def discard(self, key: str) -> Any | None:
        """主动移除并返回对象（交给调用方释放）。"""
        with self._lock:
            entry = self._items.pop(key, None)
            if entry is None:
                return None
            self._log_eviction(key, entry, "discard")
            return entry.value

    def clear(self, *, force: bool = False) -> list[str]:
        """清空缓存。默认跳过 pin 住的对象；`force=True` 才连它们一起清。"""
        with self._lock:
            removed: list[str] = []
            for key in list(self._items):
                if self._items[key].pinned and not force:
                    continue
                entry = self._items.pop(key)
                self._log_eviction(key, entry, "clear")
                removed.append(key)
            return removed

    def stats(self) -> TensorCacheStats:
        with self._lock:
            return TensorCacheStats(
                capacity_bytes=self._capacity,
                used_bytes=sum(entry.size_bytes for entry in self._items.values()),
                items=len(self._items),
                pinned=sum(1 for entry in self._items.values() if entry.pinned),
                hits=self._hits,
                misses=self._misses,
                inserts=self._inserts,
                evictions=self._evictions,
                rejects=self._rejects,
            )

    def eviction_log(self) -> list[Mapping[str, Any]]:
        """淘汰日志（有界，最多 100 条）。两类缓存各自记各自的。"""
        with self._lock:
            return list(self._eviction_log)

    # ---- 内部 -----------------------------------------------------------
    def _make_room(self, *, protect: str) -> None:
        """淘汰 LRU 直到装得下。刚放入的和 pin 住的对象都跳过。"""
        if self._capacity <= 0:
            return
        while sum(entry.size_bytes for entry in self._items.values()) > self._capacity:
            victim = next(
                (
                    key
                    for key, entry in self._items.items()
                    if not entry.pinned and key != protect
                ),
                None,
            )
            if victim is None:
                self._rejects += 1
                raise resource_exhausted(
                    "张量缓存容量不足，且其余对象都被 pin 住无法淘汰",
                    used_bytes=self.used_bytes,
                    capacity=self._capacity,
                    pinned=[key for key, entry in self._items.items() if entry.pinned],
                )
            entry = self._items.pop(victim)
            self._log_eviction(victim, entry, "evict")

    def _log_eviction(self, key: str, entry: _Entry, reason: str) -> None:
        self._evictions += 1
        self._eviction_log.append(
            {
                "key": key,
                "size_bytes": entry.size_bytes,
                "reason": reason,
                "at": self._clock(),
            }
        )
        if self._on_evict is not None:
            self._on_evict(key, entry.value, reason)
