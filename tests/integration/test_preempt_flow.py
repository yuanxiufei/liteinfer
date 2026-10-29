"""WBS-3.9 端到端：抢占 → 存状态 → 还资源 → 恢复续跑（走真实服务）。"""

from __future__ import annotations

import time

import pytest

from liteinfer.core import ErrorCode, TaskRequest, TaskStatus
from liteinfer.core.config import config_from_mapping
from liteinfer.runtime import CheckpointExecutor, EchoExecutor, TaskService

pytestmark = pytest.mark.integration


def _running_service(
    executor: object, *, start_worker: bool = True, **cache_kwargs: object
) -> TaskService:
    config = {"cache": cache_kwargs} if cache_kwargs else {}
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


def test_preemption_is_refused_without_safe_point():
    """默认不对任何任务类型开放抢占：没声明安全点的执行器，运行中的任务不接受抢占。"""
    service = _running_service(EchoExecutor(sleep_s=5.0))
    try:
        record = service.submit(TaskRequest(kind="echo", payload={"text": "hi"}))
        assert _wait_status(service, record.task_id, TaskStatus.RUNNING)
        with pytest.raises(Exception) as excinfo:
            service.preempt(record.task_id)
        assert excinfo.value.code is ErrorCode.CONFLICT
        assert "安全点" in excinfo.value.message
        assert service.get(record.task_id).status is TaskStatus.RUNNING  # 没有被偷偷暂停
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
