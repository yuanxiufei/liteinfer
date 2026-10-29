"""WBS-3.9 自动抢占：资源不足 → 让路 → 请求者跑完 → 被抢任务**自己**回到队列跑完。

刻意验证两件事，缺一件自动抢占就是 bug 不是功能：

1. 高优先级任务在资源不足时能真的抢到位置（而不是静静排队或直接失败）；
2. 被抢占的任务不会被忘掉——请求者一跑完就把位置还回来，从断点接着跑完。
   只做前半截等于把低优先级任务永久挂起。
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from liteinfer.core import ResourceBudget, TaskRequest, TaskStatus
from liteinfer.core.config import config_from_mapping
from liteinfer.runtime import CheckpointExecutor, TaskService

pytestmark = pytest.mark.integration

EVIDENCE = {
    "infer.step": (
        "tests/integration/test_preempt_flow.py::test_preempt_stops_at_safe_point_and_can_resume"
    ),
    "train.step": (
        "tests/integration/test_auto_preempt_flow.py::test_auto_preempt_victim_resumes"
    ),
}

VRAM_PER_TASK = 100


def _preempt_auto(**overrides: Any) -> dict[str, Any]:
    policy: dict[str, Any] = {
        "enabled": True,
        "auto": True,
        "verified_kinds": ["infer.step", "train.step"],
        "verification": EVIDENCE,
        "max_rounds": 2,
    }
    policy.update(overrides)
    return policy


def _service(executor: object, **preempt: Any) -> TaskService:
    """两个 worker、一块刚好只够一个任务的显存预算；`preempt` 段连通准入台账一起给。"""
    config = config_from_mapping(
        {
            "quota": {"vram_bytes": VRAM_PER_TASK},
            "queue": {
                "max_inflight": 2,
                "kind_class": {"infer.step": "inference", "train.step": "training"},
            },
            "preempt": preempt,
        }
    )
    return TaskService(config, executor=executor)


def _wait_status(service: TaskService, task_id: str, status: TaskStatus, timeout_s=5.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if service.get(task_id).status is status:
            return True
        time.sleep(0.01)
    return False


def _training_request(**kwargs: Any) -> TaskRequest:
    return TaskRequest(
        kind="train.step",
        payload={"steps": 200},
        budget=ResourceBudget(vram_bytes=VRAM_PER_TASK),
        **kwargs,
    )


def _inference_request(**kwargs: Any) -> TaskRequest:
    return TaskRequest(
        kind="infer.step",
        payload={"steps": 5},
        budget=ResourceBudget(vram_bytes=VRAM_PER_TASK),
        **kwargs,
    )


def test_auto_preempt_victim_resumes():
    """训练先占满显存 → 推理抢到它 → 推理跑完 → 训练从断点接着跑完。"""
    service = _service(
        CheckpointExecutor(steps=200, step_sleep_s=0.01), **_preempt_auto()
    )
    try:
        training = service.submit(_training_request(tenant="training"))
        assert _wait_status(service, training.task_id, TaskStatus.RUNNING)
        time.sleep(0.1)  # 让它跑几步，确保后面不是从头重跑

        inference = service.submit(_inference_request(priority=5, tenant="serving"))

        # 请求者拿到资源并跑完——没自动抢占的话这里会 resource_exhausted 失败
        finished = service.wait(inference.task_id, timeout_s=10.0)
        assert finished.status is TaskStatus.SUCCEEDED
        assert finished.result.output["steps"] == 5

        log = service.preemption_log()
        assert len(log) == 1
        assert log[0]["victim"] == training.task_id
        assert log[0]["requester"] == inference.task_id
        assert log[0]["checkpoint"] is True  # 真的拿到了安全点状态
        assert log[0]["rounds"] == 1

        # 位置还回来了：被抢的训练任务自动恢复并跑完
        resumed = service.wait(training.task_id, timeout_s=10.0)
        assert resumed.status is TaskStatus.SUCCEEDED
        assert resumed.preempt["state"]["done"] > 0  # 从断点续跑，不是从头
        assert resumed.result.output["resumed_from"] > 0
        assert service.preempted() == []  # 没有任务被落在安全点上没人管
    finally:
        service.close()


def test_auto_preempt_is_off_without_auto_flag():
    """没开 `auto` 时，资源不足就是资源不足：不许悄悄替调用方抢别人的活。"""
    service = _service(
        CheckpointExecutor(steps=200, step_sleep_s=0.01), **_preempt_auto(auto=False)
    )
    try:
        training = service.submit(_training_request())
        assert _wait_status(service, training.task_id, TaskStatus.RUNNING)

        inference = service.submit(_inference_request(priority=5))
        finished = service.wait(inference.task_id, timeout_s=10.0)
        assert finished.status is TaskStatus.FAILED
        assert finished.error is not None
        assert finished.error.code == "resource_exhausted"
        assert service.preemption_log() == []  # 一个都没抢：准入没开放就是没开放
    finally:
        service.close()


def test_equal_priority_does_not_trigger_auto_preemption():
    """同级不让路：抢占是稀缺动作，换不来确定性收益就别做。"""
    service = _service(
        CheckpointExecutor(steps=200, step_sleep_s=0.01), **_preempt_auto()
    )
    try:
        training = service.submit(_training_request(priority=5))  # 与后面的推理同优先级
        assert _wait_status(service, training.task_id, TaskStatus.RUNNING)

        inference = service.submit(_inference_request(priority=5))
        finished = service.wait(inference.task_id, timeout_s=10.0)
        assert finished.status is TaskStatus.FAILED
        assert finished.error is not None
        assert finished.error.code == "resource_exhausted"
        assert service.preemption_log() == []
    finally:
        service.close()


def test_no_victim_available_keeps_inference_failing_readably():
    """没人可以让路时，失败原因必须还是「资源不足」，不许被抢占比 try 吞成别的错误。"""
    service = _service(
        CheckpointExecutor(steps=5, step_sleep_s=0.0),
        **_preempt_auto(
            verified_kinds=["infer.step"],
            verification={"infer.step": EVIDENCE["infer.step"]},
        ),
    )
    try:
        first = service.submit(_inference_request(priority=0))
        assert _wait_status(service, first.task_id, TaskStatus.RUNNING) or first.is_terminal

        # 同类别、更高优先级：推理不许抢推理——这里没有合法 victim
        second = service.submit(_inference_request(priority=9, tenant="other"))
        finished = service.wait(second.task_id, timeout_s=10.0)
        if finished.status is TaskStatus.FAILED:
            assert finished.error is not None
            assert finished.error.code == "resource_exhausted"
        assert service.preemption_log() == []
    finally:
        service.close()
