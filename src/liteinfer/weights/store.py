"""权重与 LoRA 生命周期（WBS-2.7）。

纪律（G2 前置验收条件）：**适配器释放后，框架可观测资源回落；
不能仅以 Python 对象删除作为回收成功依据。**

因此每次真正释放都留一条 `ReleaseRecord`：含释放时刻、引用计数归零、
以及释放前后的资源采样（`sampler` 由外部注入，WBS-3 接 NVML）。
采样拿不到时也要留痕，只是标注 `sample=None`，不允许"什么都没记就算释放成功"。

引用计数语义：同一份权重被多个任务持有，只有最后一个持有者释放时才真正卸载。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..core import WeightRef, backend_error

SamplerFn = Callable[[], Mapping[str, Any]]
LoadFn = Callable[[WeightRef], Any]
UnloadFn = Callable[[WeightRef, Any], None]


@dataclass(frozen=True)
class ReleaseRecord:
    """一次真正卸载的证据。"""

    source: str
    released_at: float
    refcount_after: int
    unloaded: bool
    sample_before: dict[str, Any] | None = None
    sample_after: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "released_at": self.released_at,
            "refcount_after": self.refcount_after,
            "unloaded": self.unloaded,
            "sample_before": self.sample_before,
            "sample_after": self.sample_after,
            "error": self.error,
        }


@dataclass
class WeightHandle:
    """一份已加载权重的持有句柄。"""

    ref: WeightRef
    loaded_at: float
    refcount: int = 1
    payload: Any = None
    released_at: float | None = None

    @property
    def released(self) -> bool:
        return self.released_at is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.ref.source,
            "format": self.ref.format,
            "loaded_at": self.loaded_at,
            "refcount": self.refcount,
            "released_at": self.released_at,
        }


class WeightStore:
    """引用计数式权重仓库。

    `loader` / `unloader` / `sampler` 都从外部注入：
    本模块不依赖 torch / NVML，只负责生命周期与证据。
    """

    def __init__(
        self,
        *,
        loader: LoadFn | None = None,
        unloader: UnloadFn | None = None,
        sampler: SamplerFn | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._loader = loader
        self._unloader = unloader
        self._sampler = sampler
        self._clock = clock
        self._lock = threading.Lock()
        self._handles: dict[str, WeightHandle] = {}
        self._releases: list[ReleaseRecord] = []
        self._errors: list[str] = []

    # ---- 生命周期 ------------------------------------------------------
    def acquire(self, ref: WeightRef) -> WeightHandle:
        """加载（或复用已加载的）权重。加载失败必须报错，不允许返回残缺句柄。"""
        with self._lock:
            existing = self._handles.get(ref.source)
            if existing is not None and not existing.released:
                existing.refcount += 1
                return existing

            payload: Any = None
            if self._loader is not None:
                try:
                    payload = self._loader(ref)
                except Exception as exc:  # noqa: BLE001 - 加载失败必须转成领域错误
                    message = f"权重加载失败：{ref.source}（{exc}）"
                    self._errors.append(message)
                    raise backend_error(message, source=ref.source) from exc

            handle = WeightHandle(ref=ref, loaded_at=self._clock(), refcount=1, payload=payload)
            self._handles[ref.source] = handle
            return handle

    def release(self, handle: WeightHandle) -> ReleaseRecord | None:
        """释放一次引用；计数归零才真正卸载，并留证据。"""
        with self._lock:
            current = self._handles.get(handle.ref.source)
            if current is None or current is not handle:
                return None
            if handle.released:
                return None

            handle.refcount -= 1
            if handle.refcount > 0:
                return None

            sample_before = self._sample()
            error: str | None = None
            unloaded = False
            if self._unloader is not None:
                try:
                    self._unloader(handle.ref, handle.payload)
                    unloaded = True
                except Exception as exc:  # noqa: BLE001 - 卸载失败也要留证
                    error = f"卸载失败：{exc}"
                    self._errors.append(error)
            else:
                # 没有注入卸载器时不能假装卸载成功
                unloaded = False

            handle.released_at = self._clock()
            record = ReleaseRecord(
                source=handle.ref.source,
                released_at=handle.released_at,
                refcount_after=handle.refcount,
                unloaded=unloaded,
                sample_before=sample_before,
                sample_after=self._sample(),
                error=error,
            )
            self._releases.append(record)
            self._handles.pop(handle.ref.source, None)
            handle.payload = None
            return record

    def release_all(self) -> list[ReleaseRecord]:
        records = []
        for handle in list(self._handles.values()):
            handle.refcount = 1  # 强制走到真正卸载
            record = self.release(handle)
            if record is not None:
                records.append(record)
        return records

    # ---- 观测 ----------------------------------------------------------
    def _sample(self) -> dict[str, Any] | None:
        if self._sampler is None:
            return None
        try:
            return dict(self._sampler())
        except Exception as exc:  # noqa: BLE001 - 采样失败不该掩盖释放流程
            return {"error": str(exc)}

    def handles(self) -> list[WeightHandle]:
        return list(self._handles.values())

    def release_records(self) -> list[ReleaseRecord]:
        return list(self._releases)

    def errors(self) -> list[str]:
        return list(self._errors)

    def refcount(self, source: str) -> int:
        handle = self._handles.get(source)
        return 0 if handle is None else handle.refcount
