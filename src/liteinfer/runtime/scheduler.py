"""任务调度：类别优先级、显式优先级与公平性限制（WBS-3.10）。

口径（基线 §6 / 3.10）：

1. **类别优先级**：推理 > 评估 > 训练。类别由 kind 判定，也可用显式映射覆盖。
2. **同级内**：`TaskRequest.priority` 大者优先；同优先级按提交顺序（FIFO）。
3. **公平性**：同一 tenant 连续取走 `fairness_limit` 个之后，必须让位给别的 tenant，
   否则高优先级/大租户会把队列里的其他人饿死。`fairness_limit=0` 表示不启用。

**这不是 QoS 承诺**：它只决定「下一个取谁」，不保证时延、不抢占已在跑的任务
（抢占是 3.9，只对声明安全点的执行器开放）。
"""

from __future__ import annotations

import heapq
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..core import TaskRecord

#: 类别与调度次序：数字越小越先取。训练最后，避免长任务挡住推理。
CLASS_ORDER: dict[str, int] = {"inference": 0, "eval": 1, "training": 2}

#: 默认判定规则：按 kind 前缀归类。显式 `class_of` 映射优先于这里。
PREFIX_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("train", "lora", "finetune", "sft", "distill"), "training"),
    (("eval", "benchmark", "validate", "test", "score"), "eval"),
)


def classify_kind(kind: str, class_of: Mapping[str, str] | None = None) -> str:
    """判定任务类别。未知 kind 归 `inference`（最常见的默认），可用映射覆盖。"""
    if class_of and kind in class_of:
        return class_of[kind]
    head = kind.strip().lower()
    for prefixes, name in PREFIX_RULES:
        if head.startswith(prefixes):
            return name
    return "inference"


@dataclass(frozen=True)
class QueueItem:
    """排序键。seq 唯一，所以元组比较永远不会比到 record 上。"""

    class_rank: int
    neg_priority: int
    seq: int

    def sort_key(self) -> tuple[int, int, int]:
        return (self.class_rank, self.neg_priority, self.seq)


@dataclass
class _Node:
    item: QueueItem
    record: TaskRecord
    task_class: str


class TaskQueue:
    """按 (类别, 优先级, 提交序) 出队的优先队列，带防饿死。"""

    def __init__(
        self,
        *,
        class_of: Mapping[str, str] | None = None,
        fairness_limit: int = 0,
    ) -> None:
        if fairness_limit < 0:
            raise ValueError("fairness_limit 不能为负")
        self._class_of = class_of
        self._fairness_limit = fairness_limit
        self._heap: list[tuple[tuple[int, int, int], _Node]] = []
        self._seq = 0
        self._cond = threading.Condition()
        self._closed = False
        self._last_tenant: str | None = None
        self._streak = 0
        self._fairness_skips = 0

    # ---- 入队 / 出队 ----------------------------------------------------
    def push(self, record: TaskRecord) -> str:
        """入队，返回判定出的任务类别。"""
        task_class = classify_kind(record.request.kind, self._class_of)
        with self._cond:
            if self._closed:
                raise RuntimeError("队列已关闭，不再接受任务")
            item = QueueItem(
                class_rank=CLASS_ORDER.get(task_class, len(CLASS_ORDER)),
                neg_priority=-int(record.request.priority),
                seq=self._seq,
            )
            self._seq += 1
            heapq.heappush(self._heap, (item.sort_key(), _Node(item, record, task_class)))
            self._cond.notify()
            return task_class

    def pop(self, *, timeout: float | None = None) -> TaskRecord | None:
        """取下一个该执行的任务；队列空则等待，关闭且排空后立刻返回 None。"""
        with self._cond:
            if not self._heap and not self._closed:
                self._cond.wait(timeout)
            if not self._heap:
                return None
            node = self._pop_fair()
            self._note_tenant(node.record.request.tenant)
            return node.record

    def close(self) -> None:
        """关闭队列并唤醒所有等待者。`pop()` 会在排空后返回 None。"""
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def _pop_fair(self) -> _Node:
        """取下一个该执行的任务；命中公平性限制时跳过连续同一 tenant。"""
        if self._fairness_limit <= 0 or self._streak < self._fairness_limit:
            return heapq.heappop(self._heap)[1]

        skipped: list[tuple[tuple[int, int, int], _Node]] = []
        chosen: _Node | None = None
        while self._heap:
            key, node = heapq.heappop(self._heap)
            if node.record.request.tenant != self._last_tenant:
                chosen = node
                break
            skipped.append((key, node))

        if chosen is None:
            # 队列里只剩这一个 tenant：没有别人可让，按优先级来
            chosen = skipped.pop(0)[1]
        else:
            self._fairness_skips += 1

        for entry in skipped:
            heapq.heappush(self._heap, entry)
        return chosen

    def _note_tenant(self, tenant: str) -> None:
        if tenant == self._last_tenant:
            self._streak += 1
        else:
            self._last_tenant = tenant
            self._streak = 1

    # ---- 观测 ------------------------------------------------------------
    def __len__(self) -> int:
        with self._cond:
            return len(self._heap)

    def depth_by_class(self) -> dict[str, int]:
        with self._cond:
            counts: dict[str, int] = {}
            for _key, node in self._heap:
                counts[node.task_class] = counts.get(node.task_class, 0) + 1
            return counts

    def stats(self) -> dict[str, Any]:
        with self._cond:
            counts: dict[str, int] = {}
            for _key, node in self._heap:
                counts[node.task_class] = counts.get(node.task_class, 0) + 1
            return {
                "depth": len(self._heap),
                "by_class": counts,
                "fairness_limit": self._fairness_limit,
                "fairness_skips": self._fairness_skips,
            }
