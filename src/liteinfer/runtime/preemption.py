"""挑谁该被抢占（WBS-3.9）。

这里的每一条规则都是**可解释的策略**，不是启发式调参：

1. **类别次序**：推理能抢训练，反向不行（`inference 0 < eval 1 < training 2`）。
   训练任务最长，让它继续占着资源会把推理饿死；反过来没有收益。
2. **显式优先级**：请求者的优先级必须至少高出 victim `min_priority_gap`。
   同优先级不让路——让了也没有收益，只会换来一次昂贵的停顿与恢复。
3. **已用轮次**：`max_rounds` 用尽的任务不再被抢占，否则抢占本身制造饥饿
   （一个任务每跑到安全点就被踢下去，永远跑不完）。

`PreemptionPlanner` 只做「排一个顺序」，不负责执行抢占；
真正的状态迁移、存状态、还资源在 `TaskService` 里。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from ..core import TaskRecord, TaskRequest
from ..core.config import PreemptionConfig


def rounds_of(record: TaskRecord) -> int:
    """该任务已经被抢占过几次（没被抢过是 0）。"""
    return int((record.preempt or {}).get("rounds", 0))


@dataclass(frozen=True)
class PreemptCandidate:
    """候选 victim 与它被选中的理由，供台账与排错使用。"""

    task_id: str
    kind: str
    priority: int
    rounds: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "priority": self.priority,
            "rounds": self.rounds,
            "reason": self.reason,
        }


class PreemptionPlanner:
    """给定「来的任务」与「在跑的任务」，排出该先牺牲谁。"""

    def __init__(
        self,
        *,
        policy: PreemptionConfig,
        class_rank: Callable[[str], int],
    ) -> None:
        self._policy = policy
        self._class_rank = class_rank

    @property
    def policy(self) -> PreemptionConfig:
        return self._policy

    def candidates(
        self, incoming: TaskRequest, running: Iterable[TaskRecord]
    ) -> list[PreemptCandidate]:
        """返回有序候选（第一个最该被抢占）。不合条件的会被直接排除。"""
        if not self._policy.enabled:
            return []

        incoming_rank = self._class_rank(incoming.kind)
        picked: list[tuple[tuple[int, int, float], PreemptCandidate]] = []
        for record in running:
            victim_rank = self._class_rank(record.request.kind)
            gap = incoming.priority - record.request.priority
            if victim_rank < incoming_rank:
                continue  # 训练不许抢推理：类别次序更靠前的任务不被让路
            if gap < self._policy.min_priority_gap:
                continue  # 优先级没拉开差距，让路没有收益
            rounds = rounds_of(record)
            if rounds >= self._policy.max_rounds:
                continue  # 轮次用尽：再抢就是制造饥饿
            if not self._policy.eligible(record.request.kind):
                continue  # 没登记（或没通过恢复测试）的类型不许被抢
            candidate = PreemptCandidate(
                task_id=record.task_id,
                kind=record.request.kind,
                priority=record.request.priority,
                rounds=rounds,
                reason=(
                    f"类别次序 {victim_rank} 不低于请求者 {incoming_rank}，"
                    f"优先级差 {gap} 达到门槛 {self._policy.min_priority_gap}"
                ),
            )
            picked.append((self._sort_key(record, victim_rank), candidate))

        picked.sort(key=lambda item: item[0])
        return [candidate for _key, candidate in picked]

    def _sort_key(self, record: TaskRecord, victim_rank: int) -> tuple[int, int, float]:
        """先牺牲类别次序靠后的（训练最靠后），再看优先级最低，最后看开始得最晚。"""
        started = record.started_at if record.started_at is not None else 0.0
        return (-victim_rank, record.request.priority, -started)
