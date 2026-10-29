"""任务服务测试（WBS-1.3 / WBS-3 背压与取消的最小形态）。"""

from __future__ import annotations

import threading
import time

import pytest

from liteinfer.core import (
    ErrorCode,
    LiteInferError,
    ResourceBudget,
    TaskRequest,
    TaskStatus,
    config_from_mapping,
)
from liteinfer.runtime import (
    BlockingExecutor,
    EchoExecutor,
    FunctionExecutor,
    LeaseManager,
    TaskService,
)

pytestmark = pytest.mark.unit


def _wait_status(service: TaskService, task_id: str, status: TaskStatus, timeout_s: float = 5.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if service.get(task_id).status is status:
            return True
        time.sleep(0.01)
    return False


def test_submit_and_wait_reaches_terminal_state():
    service = TaskService(executor=FunctionExecutor({"echo": lambda ctx, req: {"ok": True}}))
    try:
        record = service.submit(TaskRequest(kind="echo", payload={"a": 1}))
        finished = service.wait(record.task_id, timeout_s=5.0)
        assert finished.status is TaskStatus.SUCCEEDED
        assert finished.result is not None
        assert finished.result.output == {"ok": True}
        assert finished.lease_id is None  # 终态后租约已释放
        assert finished.reclaim is not None and finished.reclaim["released"] is True
    finally:
        service.close()


def test_every_task_has_unique_id_and_history():
    service = TaskService(start_worker=False)
    try:
        first = service.submit(TaskRequest(kind="echo"))
        second = service.submit(TaskRequest(kind="echo"))
        assert first.task_id != second.task_id
        assert [item.target for item in first.history] == [TaskStatus.QUEUED]
    finally:
        service.close()


def test_queue_limit_rejects_instead_of_buffering():
    config = config_from_mapping({"queue": {"max_queued": 1}})
    service = TaskService(config, start_worker=False)
    try:
        service.submit(TaskRequest(kind="echo"))
        with pytest.raises(LiteInferError) as excinfo:
            service.submit(TaskRequest(kind="echo"))
        assert excinfo.value.code is ErrorCode.QUEUE_FULL
        assert excinfo.value.http_status == 429
        assert service.metrics()["counters"]["tasks.rejected"] == 1.0
    finally:
        service.close()


def test_per_kind_limit_is_independent():
    config = config_from_mapping({"queue": {"max_queued": 10, "max_queued_per_kind": 1}})
    service = TaskService(config, start_worker=False)
    try:
        service.submit(TaskRequest(kind="echo"))
        service.submit(TaskRequest(kind="other"))
        with pytest.raises(LiteInferError) as excinfo:
            service.submit(TaskRequest(kind="echo"))
        assert excinfo.value.details["scope"] == "kind"
    finally:
        service.close()


def test_client_token_makes_retry_idempotent():
    service = TaskService(start_worker=False)
    try:
        first = service.submit(TaskRequest(kind="echo", client_token="retry-1"))
        second = service.submit(TaskRequest(kind="echo", client_token="retry-1"))
        assert first.task_id == second.task_id
        assert len(service.list_tasks()) == 1
    finally:
        service.close()


def test_cancel_queued_task_completes_immediately():
    service = TaskService(start_worker=False)
    try:
        record = service.submit(TaskRequest(kind="echo"))
        ack = service.cancel(record.task_id)
        assert ack.accepted and ack.completed
        assert service.get(record.task_id).status is TaskStatus.CANCELLED
        assert service.get(record.task_id).cancel_requested is True
    finally:
        service.close()


def test_cancel_running_task_reports_accepted_then_completed():
    service = TaskService(executor=EchoExecutor(sleep_s=3.0))
    try:
        record = service.submit(TaskRequest(kind="echo"))
        assert _wait_status(service, record.task_id, TaskStatus.RUNNING)
        ack = service.cancel(record.task_id)
        assert ack.accepted is True
        assert ack.completed is False  # 「已接受取消」≠「已完成取消」
        finished = service.wait(record.task_id, timeout_s=5.0)
        assert finished.status is TaskStatus.CANCELLED
    finally:
        service.close(timeout_s=5.0)


def test_cancel_is_refused_when_executor_has_no_safe_point():
    gate = threading.Event()
    service = TaskService(executor=BlockingExecutor(gate=gate))
    try:
        record = service.submit(TaskRequest(kind="echo"))
        assert _wait_status(service, record.task_id, TaskStatus.RUNNING)
        with pytest.raises(LiteInferError) as excinfo:
            service.cancel(record.task_id)
        assert excinfo.value.code is ErrorCode.CONFLICT
        assert "安全点" in excinfo.value.message
    finally:
        gate.set()
        service.close(timeout_s=5.0)


def test_unsupported_kind_fails_with_readable_error():
    service = TaskService()
    try:
        record = service.submit(TaskRequest(kind="llm.generate"))
        finished = service.wait(record.task_id, timeout_s=5.0)
        assert finished.status is TaskStatus.FAILED
        assert finished.error is not None
        assert finished.error.code == ErrorCode.UNSUPPORTED.value
        assert "llm.generate" in finished.error.message
    finally:
        service.close()


def _boom(context: object, request: TaskRequest) -> object:
    raise RuntimeError("boom")


def test_lease_is_released_even_when_executor_raises():
    lease_manager = LeaseManager()
    service = TaskService(executor=FunctionExecutor({"echo": _boom}), lease_manager=lease_manager)
    try:
        record = service.submit(TaskRequest(kind="echo", budget=ResourceBudget(vram_bytes=64)))
        finished = service.wait(record.task_id, timeout_s=5.0)
        assert finished.status is TaskStatus.FAILED
        assert finished.error is not None and "boom" in finished.error.message
        assert lease_manager.active() == []
    finally:
        service.close()


def test_unfinished_tasks_can_be_marked_and_reclaimed():
    service = TaskService(start_worker=False)
    try:
        record = service.submit(TaskRequest(kind="echo", budget=ResourceBudget(vram_bytes=8)))
        record.transition_to(TaskStatus.RUNNING, reason="test")
        service.mark_for_recovery(record.task_id)
        assert service.get(record.task_id).status is TaskStatus.RECLAIMING
        service.reclaim(record.task_id)
        assert service.get(record.task_id).status is TaskStatus.RECLAIMED
        assert service.get(record.task_id).reclaim is not None
        assert service.unfinished() == []
    finally:
        service.close()


def test_close_marks_unfinished_tasks_for_recovery():
    gate = threading.Event()
    service = TaskService(executor=BlockingExecutor(gate=gate))
    record = service.submit(TaskRequest(kind="echo"))
    assert _wait_status(service, record.task_id, TaskStatus.RUNNING)
    service.close(timeout_s=0.1)
    final = service.get(record.task_id)
    assert final.status in (TaskStatus.RECLAIMED, TaskStatus.RECLAIMING)
    assert final.is_terminal is True or final.status is TaskStatus.RECLAIMING
    gate.set()


def test_queue_depth_and_metrics_are_reported_separately():
    service = TaskService(start_worker=False)
    try:
        service.submit(TaskRequest(kind="echo", tenant="team-a"))
        depth = service.queue_depth()
        assert depth["queued"] == 1
        assert depth["inflight"] == 0
        assert depth["by_kind"]["echo"] == 1
        assert depth["by_tenant"]["team-a"] == 1
        assert service.metrics()["counters"]["tasks.submitted"] == 1.0
    finally:
        service.close()
