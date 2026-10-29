"""抢占准入台账与 victim 选取策略（WBS-3.9 / G2）。

测的是三件事：

1. **门槛**：G2 只允许对「已实现并通过恢复测试」的类型开放；登记必须带证据。
2. **顺序**：该先牺牲谁——训练优先于推理、优先级低的先、同级不让路。
3. **上限**：轮次用尽不再抢（抢占不该变成另一种饿死）。
"""

from __future__ import annotations

from typing import Any

import pytest

from liteinfer.core import PreemptionConfig, TaskRequest, TaskStatus
from liteinfer.core.config import config_from_mapping
from liteinfer.core.models import TaskRecord
from liteinfer.runtime.preemption import PreemptionPlanner, rounds_of
from liteinfer.runtime.scheduler import CLASS_ORDER, classify_kind

pytestmark = pytest.mark.unit

EVIDENCE: dict[str, str] = {
    "infer.step": "tests/integration/test_preempt_flow.py::test_preempt_stops_at_safe_point",
    "train.step": "tests/integration/test_auto_preempt_flow.py::test_auto_preempt_victim_resumes",
}


def _class_rank(kind: str) -> int:
    """固定映射：`infer.step` 归推理，`train.step` 归训练。"""
    mapping = {"infer.step": "inference", "train.step": "training"}
    return CLASS_ORDER.get(classify_kind(kind, mapping), len(CLASS_ORDER))


def _policy(**kwargs: Any) -> PreemptionConfig:
    base: dict[str, Any] = {
        "enabled": True,
        "verified_kinds": tuple(sorted(EVIDENCE)),
        "verification": dict(EVIDENCE),
    }
    base.update(kwargs)
    return PreemptionConfig(**base)


def _planner(policy: PreemptionConfig | None = None) -> PreemptionPlanner:
    return PreemptionPlanner(policy=policy or _policy(), class_rank=_class_rank)


def _record(
    kind: str = "train.step",
    *,
    priority: int = 0,
    rounds: int = 0,
    started_at: float = 1.0,
) -> TaskRecord:
    record = TaskRecord(request=TaskRequest(kind=kind, priority=priority))
    record.transition_to(TaskStatus.QUEUED, reason="submitted")
    record.transition_to(TaskStatus.RUNNING, reason="dispatch")
    record.started_at = started_at
    if rounds:
        record.preempt = {"state": {"done": 1}, "rounds": rounds, "at": 0.0}
    return record


# ---- 准入台账 ----------------------------------------------------------------


def test_disabled_policy_produces_no_candidates():
    planner = _planner(_policy(enabled=False))
    assert planner.candidates(TaskRequest(kind="infer.step", priority=9), [_record()]) == []


def test_registration_without_evidence_is_rejected():
    policy = PreemptionConfig(enabled=True, verified_kinds=("train.step",))
    problems = policy.problems()
    assert any("verification" in item and "train.step" in item for item in problems)


def test_evidence_without_registration_is_rejected():
    policy = PreemptionConfig(enabled=True, verification={"train.step": "some-test"})
    problems = policy.problems()
    assert any("不在 verified_kinds" in item for item in problems)


def test_verified_kind_is_only_eligible_when_enabled():
    policy = _policy()
    assert policy.eligible("train.step") is True
    assert policy.eligible("unknown.kind") is False

    off = _policy(enabled=False)
    assert off.eligible("train.step") is False


def test_evidence_of_unknown_kind_is_empty_string():
    """没登记就没有证据，不许拿别的类型的证据顶替。"""
    assert _policy().evidence_of("nope") == ""


def test_illegal_policy_values_are_reported():
    policy = _policy(max_rounds=0, min_priority_gap=-1, wait_timeout_s=0.0)
    problems = policy.problems()
    assert any("max_rounds" in item for item in problems)
    assert any("min_priority_gap" in item for item in problems)
    assert any("wait_timeout_s" in item for item in problems)


def test_auto_preemption_requires_at_least_two_workers():
    """单 worker 下自动抢占必然死锁：victim 就在等资源的那个线程上。"""
    with pytest.raises(Exception) as excinfo:
        config_from_mapping(
            {
                "queue": {"max_inflight": 1},
                "preempt": {
                    "enabled": True,
                    "auto": True,
                    "verified_kinds": ["train.step"],
                    "verification": {"train.step": EVIDENCE["train.step"]},
                },
            }
        )
    # 跨段问题统一挂在 `issues` 里，message 只有「配置校验失败」
    assert any("max_inflight >= 2" in item for item in excinfo.value.details["issues"])


# ---- victim 顺序 -------------------------------------------------------------


def test_training_is_sacrificed_before_inference():
    planner = _planner()
    inference = _record("infer.step", priority=0, started_at=1.0)
    training = _record("train.step", priority=0, started_at=2.0)

    picked = planner.candidates(TaskRequest(kind="infer.step", priority=5), [inference, training])
    assert [item.kind for item in picked] == ["train.step", "infer.step"]


def test_same_priority_is_not_preempted():
    """同级不让路：让了没有收益，只换来一次昂贵的停顿与恢复。"""
    planner = _planner()
    victim = _record("train.step", priority=3)

    equal = planner.candidates(TaskRequest(kind="infer.step", priority=3), [victim])
    assert equal == []

    higher = planner.candidates(TaskRequest(kind="infer.step", priority=4), [victim])
    assert [item.task_id for item in higher] == [victim.task_id]


def test_lower_priority_is_sacrificed_first():
    planner = _planner()
    weak = _record("train.step", priority=1)
    strong = _record("train.step", priority=2)

    picked = planner.candidates(TaskRequest(kind="infer.step", priority=9), [strong, weak])
    assert [item.task_id for item in picked] == [weak.task_id, strong.task_id]


def test_inference_is_never_preempted_for_training():
    """推理跑得好好的，不许被后来的训练任务顶掉。"""
    planner = _planner()
    inference = _record("infer.step", priority=0)

    picked = planner.candidates(TaskRequest(kind="train.step", priority=9), [inference])
    assert picked == []


def test_unverified_kind_is_never_picked():
    planner = _planner()
    alien = _record("not.verified", priority=0)
    assert planner.candidates(TaskRequest(kind="infer.step", priority=9), [alien]) == []


def test_rounds_exhausted_is_excluded():
    """轮次用尽不再抢：再抢就是让这个任务永远跑不完。"""
    planner = _planner(_policy(max_rounds=2))
    exhausted = _record("train.step", priority=0, rounds=2)
    fresh = _record("train.step", priority=0, rounds=1)

    picked = planner.candidates(
        TaskRequest(kind="infer.step", priority=9), [exhausted, fresh]
    )
    assert [item.task_id for item in picked] == [fresh.task_id]


def test_candidate_carries_reason_and_rounds():
    planner = _planner()
    record = _record("train.step", priority=0, rounds=1)
    (candidate,) = planner.candidates(TaskRequest(kind="infer.step", priority=7), [record])
    assert candidate.rounds == 1
    assert candidate.priority == 0
    assert "优先级差 7" in candidate.reason


def test_rounds_of_counts_existing_preemption():
    record = _record()
    assert rounds_of(record) == 0
    record.preempt = {"state": {"done": 2}, "rounds": 3, "at": 0.0}
    assert rounds_of(record) == 3
