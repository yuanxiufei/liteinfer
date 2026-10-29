"""权重 / LoRA 生命周期：引用计数与释放证据（WBS-2.7）。"""

from __future__ import annotations

import pytest

from liteinfer.core import ErrorCode, LiteInferError, WeightRef
from liteinfer.weights import WeightStore

pytestmark = pytest.mark.unit

REF = WeightRef(source="lora-a.safetensors", format="safetensors", size_bytes=10)


def test_refcount_reuses_same_handle():
    store = WeightStore()
    first = store.acquire(REF)
    second = store.acquire(REF)
    assert first is second
    assert store.refcount(REF.source) == 2
    assert len(store.handles()) == 1


def test_only_last_release_unloads_and_leaves_evidence():
    unloaded: list[str] = []
    samples = [{"vram_bytes": 100}, {"vram_bytes": 20}]
    store = WeightStore(
        loader=lambda ref: f"payload:{ref.source}",
        unloader=lambda ref, payload: unloaded.append(ref.source),
        sampler=lambda: samples.pop(0),
    )

    handle = store.acquire(REF)
    store.acquire(REF)
    assert store.release(handle) is None  # 还有人持有，不卸载

    records = store.release_all()
    assert len(records) == 1
    record = records[0]
    assert record.unloaded is True
    assert record.refcount_after == 0
    assert unloaded == [REF.source]
    # 关键口径：释放要有可观测证据，不能只靠 Python 对象删除
    assert record.sample_before == {"vram_bytes": 100}
    assert record.sample_after == {"vram_bytes": 20}
    assert record.released_at > 0


def test_release_without_unloader_is_not_claimed_as_unloaded():
    """没有注入卸载器时，不能假装卸载成功。"""
    store = WeightStore()
    handle = store.acquire(REF)
    record = store.release(handle)
    assert record is not None
    assert record.unloaded is False
    assert record.error is None


def test_unload_failure_is_recorded_not_swallowed():
    def boom(ref: WeightRef, payload: object) -> None:
        raise RuntimeError("unload failed")

    store = WeightStore(unloader=boom)
    handle = store.acquire(REF)
    record = store.release(handle)
    assert record is not None
    assert record.unloaded is False
    assert "unload failed" in (record.error or "")
    assert store.errors()


def test_load_failure_raises_backend_error():
    def boom(ref: WeightRef) -> object:
        raise RuntimeError("cuda oom")

    store = WeightStore(loader=boom)
    with pytest.raises(LiteInferError) as excinfo:
        store.acquire(REF)
    assert excinfo.value.code is ErrorCode.BACKEND_ERROR
    assert store.refcount(REF.source) == 0


def test_sampler_error_does_not_hide_release():
    def boom() -> dict[str, int]:
        raise RuntimeError("nvml down")

    store = WeightStore(unloader=lambda ref, payload: None, sampler=boom)
    handle = store.acquire(REF)
    record = store.release(handle)
    assert record is not None
    assert record.unloaded is True
    assert record.sample_before == {"error": "nvml down"}


def test_unknown_handle_release_is_ignored():
    store = WeightStore()
    other = WeightStore()
    handle = other.acquire(REF)
    assert store.release(handle) is None
