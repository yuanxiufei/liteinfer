"""优先级调度与公平性（WBS-3.10）。"""

from __future__ import annotations

import threading

import pytest

from liteinfer.core import TaskRecord, TaskRequest
from liteinfer.runtime.scheduler import CLASS_ORDER, TaskQueue, classify_kind

pytestmark = pytest.mark.unit


def _record(kind: str, *, priority: int = 0, tenant: str = "default") -> TaskRecord:
    return TaskRecord(request=TaskRequest(kind=kind, priority=priority, tenant=tenant))


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("llm.generate", "inference"),
        ("weights.verify", "inference"),
        ("eval.perplexity", "eval"),
        ("benchmark.latency", "eval"),
        ("train.lora", "training"),
        ("finetune.sdxl", "training"),
    ],
)
def test_classify_by_prefix(kind: str, expected: str):
    assert classify_kind(kind) == expected


def test_explicit_mapping_overrides_prefix():
    mapping = {"train.lora": "inference"}  # 显式指定优先于前缀规则
    assert classify_kind("train.lora", mapping) == "inference"


def test_inference_beats_eval_beats_training():
    queue = TaskQueue()
    queue.push(_record("train.lora"))
    queue.push(_record("eval.perplexity"))
    queue.push(_record("llm.generate"))

    assert [queue.pop().request.kind for _ in range(3)] == [
        "llm.generate",
        "eval.perplexity",
        "train.lora",
    ]


def test_priority_within_same_class():
    queue = TaskQueue()
    queue.push(_record("llm.generate", priority=0))
    queue.push(_record("llm.generate", priority=10))
    queue.push(_record("llm.generate", priority=5))

    assert [item.request.priority for item in (queue.pop(), queue.pop(), queue.pop())] == [
        10,
        5,
        0,
    ]


def test_same_priority_keeps_fifo():
    queue = TaskQueue()
    records = [_record("llm.generate", priority=1) for _ in range(3)]
    for record in records:
        queue.push(record)
    assert [queue.pop() for _ in range(3)] == records


def test_fairness_limit_prevents_starvation():
    """同一 tenant 连续取到上限后，必须让位给别人，否则就是饿死。"""
    queue = TaskQueue(fairness_limit=2)
    for _ in range(4):
        queue.push(_record("llm.generate", tenant="big"))
    queue.push(_record("llm.generate", tenant="small"))

    tenants = [queue.pop().request.tenant for _ in range(5)]
    assert tenants[:3] == ["big", "big", "small"]
    assert queue.stats()["fairness_skips"] >= 1


def test_fairness_does_not_deadlock_when_only_one_tenant():
    queue = TaskQueue(fairness_limit=1)
    for _ in range(3):
        queue.push(_record("llm.generate", tenant="solo"))
    assert [queue.pop().request.tenant for _ in range(3)] == ["solo"] * 3


def test_depth_by_class_and_stats():
    queue = TaskQueue()
    queue.push(_record("train.lora"))
    queue.push(_record("train.lora"))
    queue.push(_record("llm.generate"))
    stats = queue.stats()
    assert stats["depth"] == 3
    assert stats["by_class"] == {"training": 2, "inference": 1}
    assert stats["fairness_limit"] == 0


def test_pop_returns_none_when_empty():
    assert TaskQueue().pop(timeout=0.01) is None


def test_close_wakes_waiters_and_refuses_push():
    queue = TaskQueue()
    done = threading.Event()

    def waiter() -> None:
        assert queue.pop(timeout=5.0) is None
        done.set()

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    queue.close()
    assert done.wait(timeout=2.0)
    with pytest.raises(RuntimeError):
        queue.push(_record("llm.generate"))


def test_class_order_matches_baseline():
    assert CLASS_ORDER["inference"] < CLASS_ORDER["eval"] < CLASS_ORDER["training"]
    # 未知 kind 归 inference：不假装认识它，也不把它排到训练后面挡住推理