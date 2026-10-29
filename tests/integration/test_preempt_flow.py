"""WBS-3.9 端到端：抢占 → 存状态 → 还资源 → 恢复续跑（走真实服务）。"""

from __future__ import annotations

import time

import pytest

from liteinfer.core import ErrorCode, TaskRequest, TaskStatus
from liteinfer.core.config import config_from_mapping
from liteinfer.runtime import CheckpointExecutor, TaskService

pytestmark = pytest.mark.integration


#: `checkpoint` 是这里唯一取得「已实现并通过恢复测试」资格的 kind——见 `verification`。
#: 台账为空时服务不对任何类型开放抢占，所以必须显式登记，且登记就要写取证路径。
PREEMPT_POLICY: dict[str, object] = {
    "enabled": True,
    "verified_kinds": ["checkpoint"],
    "verification": {
        "checkpoint": (
            "tests/integration/test_preempt_flow.py::"
            "test_preempt_stops_at_safe_point_and_can_resume"
        )
    },
}


def _running_service(
    executor: object,
    *,
    start_worker: bool = True,
    preempt: dict[str, object] | None = None,
    **cache_kwargs: object,
) -> TaskService:
    """默认带准入台账 `PREEMPT_POLICY`；传 `{}` 表示不开放任何抢占。"""
    policy: dict[str, object] = PREEMPT_POLICY if preempt is None else preempt
    config: dict[str, object] = {"preempt": policy} if policy else {}
    if cache_kwargs:
        config["cache"] = cache_kwargs
    return TaskService(
        config_from_mapping(config), executor=executor, start_worker=start_worker
    )


def _wait_status(service: TaskService, task_id: str, status: TaskStatus, timeout_s=5.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if service.get(task_id).status is status:
            return True
        time.sleep(0.01)
    return False


def test_preempt_stops_at_safe_point_and_can_resume():
    """恢复测试：停在安全点 → 资源归还 → 从断点接着跑完。"""
    service = _running_service(CheckpointExecutor(steps=60, step_sleep_s=0.02))
    try:
        record = service.submit(TaskRequest(kind="checkpoint", payload={"steps": 60}))
        assert _wait_status(service, record.task_id, TaskStatus.RUNNING)
        time.sleep(0.15)  # 让它先跑几步

        ack = service.preempt(record.task_id, timeout_s=5.0)
        assert ack.accepted is True
        assert ack.completed is True
        assert ack.checkpoint is True  # 真的拿到了安全点状态
        assert ack.status is TaskStatus.PREEMPTED

        paused = service.get(record.task_id)
        assert paused.preempt["state"]["done"] > 0  # 不是从头重跑
        assert paused.preempt["rounds"] == 1
        assert paused.lease_id is None  # 抢占的意义：资源先还
        assert service.queue_depth()["preempted"] == 1

        done_at = paused.preempt["state"]["done"]
        service.resume(record.task_id)
        finished = service.wait(record.task_id, timeout_s=10.0)

        assert finished.status is TaskStatus.SUCCEEDED
        assert finished.result.output["resumed_from"] == done_at  # 从断点续跑
        assert finished.result.output["steps"] == 60
    finally:
        service.close()


def test_second_preemption_keeps_counting_rounds():
    service = _running_service(CheckpointExecutor(steps=60, step_sleep_s=0.02))
    try:
        record = service.submit(TaskRequest(kind="checkpoint", payload={"steps": 60}))
        assert _wait_status(service, record.task_id, TaskStatus.RUNNING)
        time.sleep(0.1)

        service.preempt(record.task_id)
        first_done = service.get(record.task_id).preempt["state"]["done"]
        service.resume(record.task_id)
        assert _wait_status(service, record.task_id, TaskStatus.RUNNING)
        time.sleep(0.1)

        service.preempt(record.task_id)
        paused = service.get(record.task_id)
        assert paused.preempt["rounds"] == 2
        assert paused.preempt["state"]["done"] > first_done  # 第二轮接着往下跑

        service.resume(record.task_id)
        assert service.wait(record.task_id, timeout_s=10.0).status is TaskStatus.SUCCEEDED
    finally:
        service.close()


def test_preemption_is_refused_when_policy_is_off():
    """默认台账为空 ⇒ 不对任何任务类型开放抢占，连「可能会导致抢占」的请求都要拒绝。"""
    service = _running_service(CheckpointExecutor(steps=60, step_sleep_s=0.02), preempt={})
    try:
        record = service.submit(TaskRequest(kind="checkpoint", payload={"steps": 60}))
        assert _wait_status(service, record.task_id, TaskStatus.RUNNING)
        with pytest.raises(Exception) as excinfo:
            service.preempt(record.task_id)
        assert excinfo.value.code is ErrorCode.CONFLICT
        assert "抢占未启用" in excinfo.value.message
        assert service.get(record.task_id).status is TaskStatus.RUNNING
    finally:
        service.close()


def test_preemption_is_refused_without_safe_point():
    """执行器自己说了不支持，任务类型登记也没用——准入是「两个都要」不是「任一即可」。"""

    class _NoPreemptExecutor(CheckpointExecutor):
        @property
        def supports_preemption(self) -> bool:
            return False

    service = _running_service(_NoPreemptExecutor(steps=60, step_sleep_s=0.02))
    try:
        record = service.submit(TaskRequest(kind="checkpoint", payload={"steps": 60}))
        assert _wait_status(service, record.task_id, TaskStatus.RUNNING)
        with pytest.raises(Exception) as excinfo:
            service.preempt(record.task_id)
        assert excinfo.value.code is ErrorCode.CONFLICT
        assert "安全点" in excinfo.value.message
        assert service.get(record.task_id).status is TaskStatus.RUNNING  # 没有被偷偷暂停
    finally:
        service.close()


def test_unregistered_kind_is_refused_even_when_executor_declares_safe_point():
    """G2 门槛：执行器自述支持不算，类型必须已登记并通过恢复测试。"""
    service = _running_service(CheckpointExecutor(steps=60, step_sleep_s=0.02))
    try:
        record = service.submit(TaskRequest(kind="unregistered", payload={"steps": 60}))
        assert _wait_status(service, record.task_id, TaskStatus.RUNNING)
        with pytest.raises(Exception) as excinfo:
            service.preempt(record.task_id)
        assert excinfo.value.code is ErrorCode.CONFLICT
        assert "未登记" in excinfo.value.message
        assert service.get(record.task_id).status is TaskStatus.RUNNING  # 没被偷偷暂停
    finally:
        service.close()


def test_rounds_exhausted_stops_preemption():
    """轮次用尽就不再抢：抢占是让重要的先跑，不是让谁永远跑不完。"""
    policy = dict(PREEMPT_POLICY)
    policy["max_rounds"] = 1
    service = _running_service(CheckpointExecutor(steps=60, step_sleep_s=0.02), preempt=policy)
    try:
        record = service.submit(TaskRequest(kind="checkpoint", payload={"steps": 60}))
        assert _wait_status(service, record.task_id, TaskStatus.RUNNING)
        time.sleep(0.1)

        service.preempt(record.task_id)
        service.resume(record.task_id)
        assert _wait_status(service, record.task_id, TaskStatus.RUNNING)
        time.sleep(0.1)

        with pytest.raises(Exception) as excinfo:
            service.preempt(record.task_id)
        assert excinfo.value.code is ErrorCode.CONFLICT
        assert "max_rounds" in excinfo.value.message
    finally:
        service.close()


def test_preempt_refuses_queued_and_terminal_tasks():
    service = _running_service(CheckpointExecutor(steps=5, step_sleep_s=0.0), start_worker=False)
    try:
        queued = service.submit(TaskRequest(kind="checkpoint", payload={"steps": 5}))
        with pytest.raises(Exception) as excinfo:
            service.preempt(queued.task_id)
        assert excinfo.value.code is ErrorCode.CONFLICT

        service.start()
        done = service.wait(queued.task_id, timeout_s=5.0)
        assert done.status is TaskStatus.SUCCEEDED
        with pytest.raises(Exception) as excinfo:
            service.preempt(queued.task_id)
        assert excinfo.value.code is ErrorCode.CONFLICT
    finally:
        service.close()


def test_resume_without_checkpoint_is_refused():
    """没有安全点状态就不许叫"续跑"——那只能重新提交。"""
    service = _running_service(CheckpointExecutor(steps=60, step_sleep_s=0.02))
    try:
        record = service.submit(TaskRequest(kind="checkpoint", payload={"steps": 60}))
        assert _wait_status(service, record.task_id, TaskStatus.RUNNING)
        time.sleep(0.1)
        service.preempt(record.task_id)

        service.get(record.task_id).preempt = None  # 模拟状态没保存下来
        with pytest.raises(Exception) as excinfo:
            service.resume(record.task_id)
        assert excinfo.value.code is ErrorCode.CONFLICT
        assert "重新提交" in excinfo.value.message
    finally:
        service.close()


def test_preempt_returns_kv_budget():
    """抢占必须把 KV 预算也还回去，否则"腾资源"是假的。"""
    service = _running_service(
        CheckpointExecutor(steps=60, step_sleep_s=0.02), kv_tokens=100
    )
    try:
        record = service.submit(
            TaskRequest(kind="checkpoint", payload={"steps": 60, "kv_tokens": 100})
        )
        assert _wait_status(service, record.task_id, TaskStatus.RUNNING)
        time.sleep(0.1)

        service.preempt(record.task_id)
        assert service.kv_cache().stats().used_tokens == 0
        service.kv_cache().assert_no_leak()

        service.resume(record.task_id)
        assert service.wait(record.task_id, timeout_s=10.0).status is TaskStatus.SUCCEEDED
        assert service.kv_cache().stats().used_tokens == 0  # 跑完也不许留着
    finally:
        service.close()


def test_preempted_task_shows_up_as_unfinished():
    """停在安全点的任务属于"需要恢复"，不能从待处理列表里消失。"""
    service = _running_service(CheckpointExecutor(steps=60, step_sleep_s=0.02))
    try:
        record = service.submit(TaskRequest(kind="checkpoint", payload={"steps": 60}))
        assert _wait_status(service, record.task_id, TaskStatus.RUNNING)
        time.sleep(0.1)
        service.preempt(record.task_id)

        assert [item.task_id for item in service.preempted()] == [record.task_id]
        assert record.task_id in {item.task_id for item in service.unfinished()}
        assert service.get(record.task_id).to_dict()["needs_recovery"] is True
    finally:
        service.close()
