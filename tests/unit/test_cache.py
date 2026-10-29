"""两类缓存：KV 预算与张量特征 LRU（WBS-3.5 / 3.6）。"""

from __future__ import annotations

import pytest

from liteinfer.core import ErrorCode, LiteInferError
from liteinfer.runtime.cache import KVCacheBudget, TensorFeatureCache

pytestmark = pytest.mark.unit


# ------------------------------------------------------------------ KV 缓存


def test_zero_capacity_means_unlimited():
    budget = KVCacheBudget()
    assert budget.reserve(10_000) is True
    assert budget.stats().used_tokens == 10_000


def test_over_budget_is_rejected_not_queued():
    budget = KVCacheBudget(capacity_tokens=100)
    budget.reserve(80)
    with pytest.raises(LiteInferError) as excinfo:
        budget.reserve(30)
    assert excinfo.value.code is ErrorCode.RESOURCE_EXHAUSTED
    assert excinfo.value.http_status == 503
    assert excinfo.value.details["capacity"] == 100
    assert budget.stats().used_tokens == 80  # 拒绝不能顺手把预算改掉


def test_release_recycles_and_never_goes_negative():
    budget = KVCacheBudget(capacity_tokens=100)
    budget.reserve(50, owner="t1")
    budget.release(80, owner="t1")  # 多还：按 0 截断，不许变负数

    stats = budget.stats()
    assert stats.used_tokens == 0
    assert stats.recycled_tokens == 80
    assert budget.owners() == {}


def test_owner_is_tracked_until_released():
    budget = KVCacheBudget()
    budget.reserve(10, owner="t1")
    budget.reserve(20, owner="t2")
    assert budget.owners() == {"t1": 10, "t2": 20}
    budget.release(10, owner="t1")
    assert budget.owners() == {"t2": 20}


def test_evict_counts_and_lowers_usage():
    budget = KVCacheBudget(capacity_tokens=100)
    budget.reserve(90)
    budget.evict(40)
    stats = budget.stats()
    assert stats.used_tokens == 50
    assert stats.evictions == 1
    assert stats.evicted_tokens == 40


def test_hit_miss_counters():
    budget = KVCacheBudget()
    budget.hit()
    budget.hit()
    budget.miss()
    stats = budget.stats()
    assert (stats.hits, stats.misses) == (2, 1)


def test_bad_arguments_are_rejected():
    with pytest.raises(LiteInferError):
        KVCacheBudget(capacity_tokens=-1)
    budget = KVCacheBudget()
    with pytest.raises(LiteInferError):
        budget.reserve(0)
    with pytest.raises(LiteInferError):
        budget.evict(-5)


def test_assert_no_leak():
    budget = KVCacheBudget()
    budget.reserve(5, owner="t1")
    with pytest.raises(LiteInferError) as excinfo:
        budget.assert_no_leak()
    assert "t1" in excinfo.value.details["owners"]
    budget.release(5, owner="t1")
    budget.assert_no_leak()


def test_peak_tokens_recorded():
    budget = KVCacheBudget()
    budget.reserve(70)
    budget.reserve(30)
    budget.release(100)
    assert budget.stats().peak_tokens == 100


# -------------------------------------------------------- 张量特征缓存


def test_lru_evicts_least_recently_used():
    evicted: list[str] = []
    cache = TensorFeatureCache(
        capacity_bytes=100, on_evict=lambda key, _value, _reason: evicted.append(key)
    )

    cache.put("a", "A", 40)
    cache.put("b", "B", 40)
    assert cache.get("a") == "A"  # a 变新
    cache.put("c", "C", 40)  # 装不下，淘汰最旧的 b

    assert evicted == ["b"]
    assert cache.get("b") is None
    assert cache.get("a") == "A"
    assert cache.used_bytes == 80


def test_pinned_objects_are_not_evicted():
    cache = TensorFeatureCache(capacity_bytes=100)
    cache.put("held", object(), 60)
    cache.pin("held")
    cache.put("other", object(), 30)
    with pytest.raises(LiteInferError) as excinfo:
        cache.put("third", object(), 50)  # 淘汰 other 也不够，held 又不能动
    assert excinfo.value.code is ErrorCode.RESOURCE_EXHAUSTED
    assert "held" in excinfo.value.details["pinned"]
    assert cache.get("held") is not None


def test_put_rolls_back_when_it_does_not_fit():
    cache = TensorFeatureCache(capacity_bytes=50)
    cache.put("keep", object(), 40)
    cache.pin("keep")
    with pytest.raises(LiteInferError):
        cache.put("big", object(), 40)
    assert cache.get("big") is None  # 不许留下"放进去一半"的中间态
    assert cache.used_bytes == 40


def test_object_bigger_than_whole_cache_is_rejected():
    cache = TensorFeatureCache(capacity_bytes=10)
    with pytest.raises(LiteInferError) as excinfo:
        cache.put("huge", object(), 100)
    assert excinfo.value.code is ErrorCode.RESOURCE_EXHAUSTED
    assert cache.stats().rejects == 1


def test_unlimited_capacity_never_evicts():
    cache = TensorFeatureCache()  # 0 = 不设限
    for index in range(50):
        cache.put(f"k{index}", index, 1_000)
    assert cache.stats().items == 50
    assert cache.stats().evictions == 0


def test_hit_miss_and_update_semantics():
    cache = TensorFeatureCache()
    cache.put("k", 1, 10)
    cache.get("missing")
    assert cache.get("k") == 1
    cache.put("k", 2, 20)  # 覆盖：不算新插入
    assert cache.get("k") == 2
    stats = cache.stats()
    assert (stats.hits, stats.misses, stats.inserts) == (2, 1, 1)
    assert stats.used_bytes == 20


def test_discard_returns_object_and_frees_bytes():
    cache = TensorFeatureCache()
    cache.put("k", "value", 30)
    assert cache.discard("k") == "value"
    assert cache.used_bytes == 0
    assert cache.discard("k") is None


def test_clear_skips_pinned_unless_forced():
    cache = TensorFeatureCache()
    cache.put("a", 1, 10)
    cache.put("b", 2, 10)
    cache.pin("b")
    assert cache.clear() == ["a"]
    assert cache.stats().items == 1
    assert cache.clear(force=True) == ["b"]
    assert cache.stats().items == 0


def test_eviction_log_is_bounded():
    cache = TensorFeatureCache(capacity_bytes=10)
    for index in range(150):
        cache.put(f"k{index}", index, 10)
    log = cache.eviction_log()
    assert len(log) == 100  # 有界，不会无限增长
    assert {"key", "size_bytes", "reason", "at"} <= set(log[0])


def test_pin_unknown_key_is_not_found():
    cache = TensorFeatureCache()
    with pytest.raises(LiteInferError) as excinfo:
        cache.pin("nope")
    assert excinfo.value.code is ErrorCode.NOT_FOUND
