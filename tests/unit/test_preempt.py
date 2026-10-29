"""安全抢占的状态机、上下文与准入（WBS-3.9）。"""

from __future__ import annotations

import threading

import pytest

from liteinfer.core import (
    ErrorCode,
    TaskPreempted,
    TaskStatus,
    can_transition,
    conflict,
    needs_recovery,
)
from liteinfer.core.models import TaskRecord, TaskRequest
from liteinfer.runtime.executor import ExecutionContext

pytestmark = pytest.mark.unit


def test_preempt_transitions():
    assert can_transition(TaskStatus.RUNNING, TaskStatus.PREEMPTING)
    assert can_transition(TaskStatus.PREEMPTING, TaskStatus.PREEMPTED)
    assert can_transition(TaskStatus.PREEMPTED, TaskStatus.QUEUED)
    assert can_transition(TaskStatus.PREEMPTED, TaskStatus.RECLAIMING)


def test_preempted_needs_recovery():
    """停在安全点的任务，进程重启后必须显式恢复或回收，不能当它还在跑。"""
    assert needs_recovery(TaskStatus.PREEMPTED)
    assert needs_recovery(TaskStatus.PREEMPTING)


def test_illegal_preempt_transition_is_refused():
    record = TaskRecord(request=TaskRequest(kind="echo"))  # SUBMITTED
    with pytest.raises(Exception) as excinfo:
        record.transition_to(TaskStatus.PREEMPTED, reason="bad")
    assert excinfo.value.code is ErrorCode.CONFLICT


def test_checkpoint_is_silent_without_preempt_request():
    context = ExecutionContext(task_id="t1")
    context.checkpoint({"step": 3})  # 没有抢占请求 → 什么都不发生


def test_checkpoint_raises_with_state_when_preempt_requested():
    event = threading.Event()
    context = ExecutionContext(task_id="t1", preempt_event=event)
    event.set()

    with pytest.raises(TaskPreempted) as excinfo:
        context.checkpoint({"step": 7})

    assert excinfo.value.code is ErrorCode.PREEMPTED
    assert excinfo.value.state == {"step": 7}
    assert excinfo.value.retryable is True  # 停在安全点不等于失败，能续跑


def test_resume_state_is_carried():
    assert ExecutionContext(task_id="t1", resume_state={"done": 4}).resume_state == {"done": 4}
    assert ExecutionContext(task_id="t2").resume_state is None


def test_preempted_is_not_terminal_for_the_record():
    record = TaskRecord(request=TaskRequest(kind="echo"))
    record.transition_to(TaskStatus.QUEUED, reason="submitted")
    record.transition_to(TaskStatus.RUNNING, reason="dispatch")
    record.transition_to(TaskStatus.PREEMPTING, reason="preempt:requested")
    record.transition_to(TaskStatus.PREEMPTED, reason="preempt:safepoint")
    assert record.is_terminal is False
    assert record.done.is_set() is False  # 等恢复，不是等终态


def test_conflict_helper_shape():
    error = conflict("不能抢占", task_id="t1")
    assert error.code is ErrorCode.CONFLICT
    assert error.http_status == 409
