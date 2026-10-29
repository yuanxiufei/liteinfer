"""WBS-3.5 / 3.6 / 3.4 端到端：缓存预算、调度许可与采样（走真实服务）。"""

from __future__ import annotations

import threading
import time

import pytest

from liteinfer.bootstrap import build_service
from liteinfer.core import ErrorCode, TaskRequest, TaskStatus
from liteinfer.core.config import config_from_mapping
from liteinfer.runtime import BlockingExecutor, EchoExecutor, TaskService

pytestmark = pytest.mark.integration


def _service(**config_kwargs: object) -> TaskService:
    return TaskService(
        config_from_mapping(config_kwargs), executor=EchoExecutor(), start_worker=False
    )


def _reclaim_of(service: TaskService, task_id: str, *, timeout_s: float = 5.0) -> dict:
    """终态先落，回收证据紧随其后——所以这里要等证据，不能假定同一步就位。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        reclaim = service.get(task_id).reclaim
        if reclaim:
            return reclaim
        time.sleep(0.02)
    raise AssertionError("释放证据迟迟没有落盘")


def test_kv_budget_denies_dispatch_before_execution():
    """3.5 调度许可：预算不够就在开跑前拒绝，不许带着"也许够"的预算执行。

    要并发才会撞上：第一个任务占住 90 且没跑完，第二个要 50 就该被拒。
    """
    gate = threading.Event()
    config = config_from_mapping(
        {"queue": {"max_inflight": 2}, "cache": {"kv_tokens": 100}}
    )
    service = TaskService(config, BlockingExecutor(gate=gate), start_worker=False)
    try:
        service.start()
        holder = service.submit(TaskRequest(kind="echo", payload={"kv_tokens": 90}))
        deadline = time.time() + 5.0
        while time.time() < deadline and service.get(holder.task_id).status is not (
            TaskStatus.RUNNING
        ):
            time.sleep(0.02)

        second = service.submit(TaskRequest(kind="echo", payload={"kv_tokens": 50}))
        denied = service.wait(second.task_id, timeout_s=5.0)
        assert denied.status is TaskStatus.FAILED
        assert denied.error.code == ErrorCode.RESOURCE_EXHAUSTED.value
        # 在途任务会归还预算，所以标可重试；错误信息必须说清"要多少、已用多少、上限多少"
        assert denied.error.details == {
            "requested": 50,
            "used": 90,
            "capacity": 100,
            "owner": second.task_id,
            "hint": denied.error.details["hint"],
        }

        gate.set()
        assert service.wait(holder.task_id, timeout_s=5.0).status is TaskStatus.SUCCEEDED
    finally:
        gate.set()
        service.close()


def test_kv_budget_is_returned_after_task_ends():
    """任务结束必须归还预算，否则预算会随任务数单调泄漏。"""
    service = _service(cache={"kv_tokens": 100})
    try:
        service.start()
        for _ in range(5):
            record = service.submit(TaskRequest(kind="echo", payload={"kv_tokens": 100}))
            assert service.wait(record.task_id, timeout_s=5.0).status is TaskStatus.SUCCEEDED
        stats = service.kv_cache().stats()
        assert stats.used_tokens == 0
        assert stats.recycled_tokens == 500
        assert stats.peak_tokens == 100
        service.kv_cache().assert_no_leak()
    finally:
        service.close()


def test_kv_budget_is_returned_even_when_task_is_cancelled():
    """非正常路径也要归还：中断任务留下的预算是最常见的一种泄漏。"""
    service = _service(cache={"kv_tokens": 100})
    try:
        service.start()
        record = service.submit(TaskRequest(kind="echo", payload={"kv_tokens": 100}))
        service.cancel(record.task_id)
        service.wait(record.task_id, timeout_s=5.0)
        assert service.kv_cache().stats().used_tokens == 0
    finally:
        service.close()


def test_illegal_kv_tokens_is_rejected_before_running():
    service = _service(cache={"kv_tokens": 100})
    try:
        service.start()
        record = service.submit(TaskRequest(kind="echo", payload={"kv_tokens": "lots"}))
        done = service.wait(record.task_id, timeout_s=5.0)
        assert done.status is TaskStatus.FAILED
        assert done.error.code == ErrorCode.INVALID_ARGUMENT.value
    finally:
        service.close()


def test_tasks_without_kv_declaration_do_not_occupy_budget():
    service = _service(cache={"kv_tokens": 10})
    try:
        service.start()
        record = service.submit(TaskRequest(kind="echo", payload={"text": "hi"}))
        assert service.wait(record.task_id, timeout_s=5.0).status is TaskStatus.SUCCEEDED
        assert service.kv_cache().stats().used_tokens == 0
    finally:
        service.close()


def test_two_caches_have_separate_budgets_and_metrics():
    """基线 §6.2：预算分开、指标分开、淘汰日志分开。"""
    service = _service(cache={"kv_tokens": 100, "tensor_bytes": 64})
    try:
        service.tensor_cache().put("a", object(), 40)
        service.tensor_cache().put("b", object(), 40)  # 装不下，淘汰 a
        stats = service.cache_stats()

        assert stats["kv"]["capacity_tokens"] == 100
        assert stats["kv"]["used_tokens"] == 0  # 没有任务声明 kv_tokens
        assert stats["tensor"]["capacity_bytes"] == 64
        assert stats["tensor"]["used_bytes"] == 40
        assert stats["tensor"]["evictions"] == 1
        assert service.tensor_cache().eviction_log()[0]["key"] == "a"
    finally:
        service.close()


def test_cache_stats_are_exposed_in_metrics():
    service = _service(cache={"kv_tokens": 8, "tensor_bytes": 16})
    try:
        assert set(service.metrics()["cache"]) == {"kv", "tensor"}
    finally:
        service.close()


def test_release_evidence_carries_real_meter_sample():
    """3.4：释放证据里带真实采样，而不是 declared 占位。"""
    service = build_service()
    try:
        record = service.submit(TaskRequest(kind="echo", payload={"text": "hi"}))
        done = service.wait(record.task_id, timeout_s=5.0)
        assert done.status is TaskStatus.SUCCEEDED
        sample = _reclaim_of(service, record.task_id)["released_sample"]
        assert "disk_usage" in sample["source"]  # 显存采样待 NVML，故不要求 vram
        assert sample["ssd_bytes"] > 0
    finally:
        service.close()
