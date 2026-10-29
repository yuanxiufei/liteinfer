"""任务状态机（WBS-1.3）。

状态迁移只允许走 `TRANSITIONS` 白名单；非法迁移抛 `CONFLICT`，
绝不静默改写状态。终态之外的非活动状态（RUNNING / CANCELLING / RECLAIMING）
在进程退出后属于「需要恢复」，必须能被显式标记与回收。
"""

from __future__ import annotations

from enum import StrEnum

from .errors import conflict


class TaskStatus(StrEnum):
    SUBMITTED = "submitted"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    RECLAIMING = "reclaiming"
    RECLAIMED = "reclaimed"
    #: 抢占已受理，但执行器还没走到安全点（WBS-3.9）。
    PREEMPTING = "preempting"
    #: 已停在安全点、状态已保存、资源已归还，等待被恢复（WBS-3.9）。
    PREEMPTED = "preempted"


TERMINAL: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.RECLAIMED,
    }
)

#: 进程崩溃后可能残留的状态，重启时必须显式标记恢复或回收。
#: PREEMPTED 也在此列：任务停在安全点上，重启后必须显式恢复或回收，
#: 不能默认它还在跑（资源其实已经还了）。
NEEDS_RECOVERY: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.RUNNING,
        TaskStatus.CANCELLING,
        TaskStatus.RECLAIMING,
        TaskStatus.PREEMPTING,
        TaskStatus.PREEMPTED,
    }
)

TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.SUBMITTED: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED, TaskStatus.FAILED}),
    TaskStatus.QUEUED: frozenset(
        {TaskStatus.RUNNING, TaskStatus.CANCELLING, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLING,
            TaskStatus.CANCELLED,
            TaskStatus.RECLAIMING,
            TaskStatus.PREEMPTING,
            TaskStatus.PREEMPTED,
        }
    ),
    #: 抢占请求发出后，执行器仍可能在走到安全点之前就正常跑完——所以允许 SUCCEEDED。
    TaskStatus.PREEMPTING: frozenset(
        {
            TaskStatus.PREEMPTED,
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.RECLAIMING,
        }
    ),
    #: 停在安全点上：可以恢复（回到 QUEUED），也可以放弃（取消 / 失败 / 回收）。
    TaskStatus.PREEMPTED: frozenset(
        {
            TaskStatus.QUEUED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.RECLAIMING,
        }
    ),
    TaskStatus.CANCELLING: frozenset(
        {TaskStatus.CANCELLED, TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.RECLAIMING}
    ),
    TaskStatus.RECLAIMING: frozenset(
        {TaskStatus.RECLAIMED, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
    TaskStatus.RECLAIMED: frozenset(),
}


def can_transition(source: TaskStatus, target: TaskStatus) -> bool:
    return target in TRANSITIONS[source]


def ensure_transition(source: TaskStatus, target: TaskStatus, *, task_id: str = "") -> None:
    if not can_transition(source, target):
        raise conflict(
            f"非法状态迁移：{source.value} → {target.value}",
            task_id=task_id,
            source=source.value,
            target=target.value,
        )


def is_terminal(status: TaskStatus) -> bool:
    return status in TERMINAL


def needs_recovery(status: TaskStatus) -> bool:
    return status in NEEDS_RECOVERY
