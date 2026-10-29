"""资源租约测试（WBS-1 / WBS-3）。

记住边界：这是逻辑配额与记账，不是 GPU 显存硬隔离，测试只验证记账行为。
"""

from __future__ import annotations

import pytest

from liteinfer.core import ErrorCode, LiteInferError, ResourceBudget
from liteinfer.runtime import CallbackMeter, LeaseManager, ResourceSample

pytestmark = pytest.mark.unit


def test_acquire_and_release_records_samples():
    manager = LeaseManager(CallbackMeter(lambda: ResourceSample(vram_bytes=1024, ram_bytes=2048)))
    lease = manager.acquire("task-1", ResourceBudget(vram_bytes=512, ram_bytes=512, ssd_bytes=8))

    assert lease.active
    assert manager.outstanding().vram_bytes == 512
    assert manager.outstanding().ssd_bytes == 8
    assert manager.peak().vram_bytes == 512

    sample = manager.release(lease.lease_id)
    assert sample.vram_bytes == 1024
    assert manager.active() == []
    assert manager.outstanding() == ResourceBudget()
    assert manager.peak().vram_bytes == 512  # 峰值不回落


def test_capacity_is_enforced_per_resource():
    manager = LeaseManager(capacity=ResourceBudget(vram_bytes=1000, ram_bytes=1000))
    manager.acquire("task-1", ResourceBudget(vram_bytes=600))
    with pytest.raises(LiteInferError) as excinfo:
        manager.acquire("task-2", ResourceBudget(vram_bytes=600))
    assert excinfo.value.code is ErrorCode.RESOURCE_EXHAUSTED
    assert excinfo.value.http_status == 503


def test_zero_capacity_means_unlimited():
    manager = LeaseManager()
    for index in range(5):
        manager.acquire(f"task-{index}", ResourceBudget(vram_bytes=10_000))
    assert len(manager.active()) == 5


def test_double_release_is_rejected():
    manager = LeaseManager()
    lease = manager.acquire("task-1", ResourceBudget(vram_bytes=1))
    manager.release(lease.lease_id)
    with pytest.raises(LiteInferError) as excinfo:
        manager.release(lease.lease_id)
    assert excinfo.value.code is ErrorCode.NOT_FOUND


def test_assert_no_leak_detects_outstanding_lease():
    manager = LeaseManager()
    manager.acquire("task-1", ResourceBudget(vram_bytes=1))
    with pytest.raises(LiteInferError) as excinfo:
        manager.assert_no_leak()
    assert excinfo.value.details["task_ids"] == ["task-1"]
