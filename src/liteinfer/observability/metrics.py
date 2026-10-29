"""进程内指标注册表（WBS-1.7 / WBS-3）。

先提供可断言的进程内指标（计数、瞬时值、采样序列），
外部监控系统（Prometheus 等）的导出留到具备实测基线之后，
不在未验证前引入运行时依赖。
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Counter:
    name: str
    value: float = 0.0

    def inc(self, amount: float = 1.0) -> None:
        self.value += amount


@dataclass
class Gauge:
    name: str
    value: float = 0.0

    def set(self, value: float) -> None:
        self.value = value


@dataclass
class Samples:
    """有界采样序列，用于延迟 / 显存等需要看分布而非单点的量。"""

    name: str
    maxlen: int = 512
    values: deque[float] = field(init=False)

    def __post_init__(self) -> None:
        self.values = deque(maxlen=self.maxlen)

    def observe(self, value: float) -> None:
        self.values.append(value)

    def snapshot(self) -> dict[str, Any]:
        items = list(self.values)
        if not items:
            return {"count": 0}
        ordered = sorted(items)

        def percentile(ratio: float) -> float:
            index = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
            return ordered[index]

        return {
            "count": len(items),
            "min": ordered[0],
            "max": ordered[-1],
            "mean": sum(items) / len(items),
            "p50": percentile(0.50),
            "p95": percentile(0.95),
        }


class MetricRegistry:
    """线程安全的简单注册表。快照只含数值，可直接 JSON 序列化。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counters: dict[str, Counter] = {}
        self._gauges: dict[str, Gauge] = {}
        self._samples: dict[str, Samples] = {}

    def counter(self, name: str) -> Counter:
        with self._lock:
            return self._counters.setdefault(name, Counter(name))

    def gauge(self, name: str) -> Gauge:
        with self._lock:
            return self._gauges.setdefault(name, Gauge(name))

    def samples(self, name: str, *, maxlen: int = 512) -> Samples:
        with self._lock:
            existing = self._samples.get(name)
            if existing is None:
                existing = Samples(name, maxlen=maxlen)
                self._samples[name] = existing
            return existing

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "counters": {name: item.value for name, item in self._counters.items()},
                "gauges": {name: item.value for name, item in self._gauges.items()},
                "samples": {name: item.snapshot() for name, item in self._samples.items()},
            }
