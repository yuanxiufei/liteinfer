"""静态配额与弹性借用（WBS-3.3 / 3.8）。"""

from __future__ import annotations

import pytest

from liteinfer.core import ErrorCode, LiteInferConfig, LiteInferError, ResourceBudget
from liteinfer.core.config import QuotaConfig, config_from_mapping
from liteinfer.runtime import LeaseManager, ResourceSample

pytestmark = pytest.mark.unit


def test_zero_means_unlimited_not_zero_capacity():
    quota = QuotaConfig()
    assert quota.usable() == {"vram_bytes": 0, "ram_bytes": 0, "ssd_bytes": 0}


def test_reserved_space_is_deducted():
    quota = QuotaConfig(vram_bytes=1000, reserved_vram_bytes=200)
    assert quota.usable()["vram_bytes"] == 800


def test_reserved_must_be_smaller_than_capacity():
    quota = QuotaConfig(vram_bytes=100, reserved_vram_bytes=100)
    assert any("reserved_vram_bytes" in item for item in quota.problems())


def test_negative_values_are_rejected():
    assert QuotaConfig(vram_bytes=-1).problems()


def test_quota_section_is_accepted_and_unknown_keys_rejected():
    config = config_from_mapping({"quota": {"vram_bytes": 1024, "reserved_vram_bytes": 24}})
    assert config.quota.vram_bytes == 1024
    with pytest.raises(LiteInferError):
        config_from_mapping({"quota": {"nonsense": 1}})


def test_service_uses_quota_capacity():
    """配置项必须真生效：配额从 QuotaConfig 流到 LeaseManager。"""
    from liteinfer.runtime import TaskService

    service = TaskService(
        config_from_mapping({"quota": {"vram_bytes": 1000, "reserved_vram_bytes": 200}}),
        start_worker=False,
    )
    try:
        assert service._lease_manager.capacity.vram_bytes == 800  # noqa: SLF001 - 配额接线断言
    finally:
        service.close()


def test_over_capacity_without_borrow_is_rejected():
    manager = LeaseManager(
        capacity=ResourceBudget(vram_bytes=100),
        borrow_limit=ResourceBudget(),  # 未开借用
    )
    manager.acquire("t1", ResourceBudget(vram_bytes=60))
    with pytest.raises(LiteInferError) as excinfo:
        manager.acquire("t2", ResourceBudget(vram_bytes=60))
    assert excinfo.value.code is ErrorCode.RESOURCE_EXHAUSTED


def test_borrow_covers_only_the_excess_and_is_traced():
    """只借超出部分，且借用者一直可查。"""
    manager = LeaseManager(
        capacity=ResourceBudget(vram_bytes=100),
        borrow_limit=ResourceBudget(vram_bytes=100),
    )
    manager.acquire("t1", ResourceBudget(vram_bytes=100))
    lease = manager.acquire("t2", ResourceBudget(vram_bytes=60))

    assert lease.borrowed.vram_bytes == 60  # 基础配额已占满，这 60 全是借的
    assert manager.borrowed().vram_bytes == 60
    borrowers = manager.borrowers()
    assert [item.task_id for item in borrowers] == ["t2"]


def test_only_the_excess_is_borrowed():
    """基线 3.8：只借用基础配额之外的部分，不是把整个请求算成借用。"""
    manager = LeaseManager(
        capacity=ResourceBudget(vram_bytes=100),
        borrow_limit=ResourceBudget(vram_bytes=100),
    )
    manager.acquire("t1", ResourceBudget(vram_bytes=60))
    lease = manager.acquire("t2", ResourceBudget(vram_bytes=60))  # 60+60=120，超出 20

    assert lease.borrowed.vram_bytes == 20
    assert manager.borrowed().vram_bytes == 20


def test_borrow_beyond_limit_is_rejected():
    manager = LeaseManager(
        capacity=ResourceBudget(vram_bytes=100),
        borrow_limit=ResourceBudget(vram_bytes=10),
    )
    manager.acquire("t1", ResourceBudget(vram_bytes=100))
    with pytest.raises(LiteInferError) as excinfo:
        manager.acquire("t2", ResourceBudget(vram_bytes=50))
    assert excinfo.value.code is ErrorCode.RESOURCE_EXHAUSTED
    assert excinfo.value.details["borrow_limit"]["vram_bytes"] == 10


def test_borrow_is_returned_with_lease():
    manager = LeaseManager(
        capacity=ResourceBudget(vram_bytes=100),
        borrow_limit=ResourceBudget(vram_bytes=100),
    )
    manager.acquire("t1", ResourceBudget(vram_bytes=100))
    lease = manager.acquire("t2", ResourceBudget(vram_bytes=40))
    assert manager.borrowed().vram_bytes == 40

    manager.release(lease.lease_id)
    assert manager.borrowed().vram_bytes == 0
    assert manager.borrowers() == []


def test_peak_and_samples_still_tracked_with_borrow():
    class Meter:
        def sample(self) -> ResourceSample:
            return ResourceSample(vram_bytes=42, source="fake")

    manager = LeaseManager(
        Meter(),
        capacity=ResourceBudget(vram_bytes=10),
        borrow_limit=ResourceBudget(vram_bytes=20),
    )
    manager.acquire("t1", ResourceBudget(vram_bytes=25))
    assert manager.peak().vram_bytes == 25


def test_config_defaults_keep_core_runnable():
    """默认配置不能因为加了配额就把核心能力关掉。"""
    config = LiteInferConfig()
    config.validate()
    assert config.quota.borrow_limit() == {"vram_bytes": 0, "ram_bytes": 0, "ssd_bytes": 0}
