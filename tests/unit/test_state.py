"""状态机与领域模型测试（WBS-1.2 / 1.3）。"""

from __future__ import annotations

import pytest

from liteinfer.core import (
    ErrorCode,
    LiteInferError,
    TaskRecord,
    TaskRequest,
    TaskStatus,
    can_transition,
    ensure_transition,
    is_terminal,
    needs_recovery,
    request_from_mapping,
)

pytestmark = pytest.mark.unit


def test_terminal_states():
    assert is_terminal(TaskStatus.SUCCEEDED)
    assert is_terminal(TaskStatus.CANCELLED)
    assert not is_terminal(TaskStatus.RUNNING)
    assert needs_recovery(TaskStatus.RUNNING)
    assert needs_recovery(TaskStatus.CANCELLING)
    assert not needs_recovery(TaskStatus.SUCCEEDED)


def test_legal_transitions():
    assert can_transition(TaskStatus.SUBMITTED, TaskStatus.QUEUED)
    assert can_transition(TaskStatus.QUEUED, TaskStatus.RUNNING)
    assert can_transition(TaskStatus.RUNNING, TaskStatus.CANCELLING)
    assert can_transition(TaskStatus.CANCELLING, TaskStatus.CANCELLED)


def test_illegal_transition_raises_conflict():
    with pytest.raises(LiteInferError) as excinfo:
        ensure_transition(TaskStatus.SUCCEEDED, TaskStatus.RUNNING, task_id="task-x")
    assert excinfo.value.code is ErrorCode.CONFLICT
    assert excinfo.value.http_status == 409


def test_record_transition_records_history():
    record = TaskRecord(request=TaskRequest(kind="echo"))
    record.transition_to(TaskStatus.QUEUED, reason="submitted")
    record.transition_to(TaskStatus.RUNNING, reason="dispatch")
    record.transition_to(TaskStatus.SUCCEEDED, reason="done")

    assert record.is_terminal
    assert record.started_at is not None
    assert record.finished_at is not None
    assert [item.target for item in record.history] == [
        TaskStatus.QUEUED,
        TaskStatus.RUNNING,
        TaskStatus.SUCCEEDED,
    ]
    payload = record.to_dict()
    assert payload["status"] == "succeeded"
    assert payload["needs_recovery"] is False


def test_request_from_mapping_rejects_unknown_field():
    with pytest.raises(LiteInferError) as excinfo:
        request_from_mapping({"kind": "echo", "max_tokens": 128})
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT
    assert "max_tokens" in excinfo.value.details["unknown"]


def test_request_from_mapping_requires_kind():
    with pytest.raises(LiteInferError):
        request_from_mapping({"payload": {}})


def test_request_from_mapping_builds_model_and_budget():
    request = request_from_mapping(
        {
            "kind": "llm.generate",
            "payload": {"prompt": "hi"},
            "model": {"model_id": "qwen", "version": "1.0", "kind": "llm"},
            "budget": {"vram_bytes": 1024},
            "priority": 2,
        }
    )
    assert request.model is not None
    assert request.model.model_id == "qwen"
    assert request.budget.vram_bytes == 1024
    assert request.budget.ram_bytes == 0
    assert request.priority == 2
