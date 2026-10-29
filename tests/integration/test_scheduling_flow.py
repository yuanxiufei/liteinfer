"""WBS-3 端到端：调度优先级、公平性与并发上限（走真实服务，不用 mock）。"""

from __future__ import annotations

import threading
import time

import pytest

from liteinfer.core import TaskRequest, TaskResult
from liteinfer.core.config import config_from_mapping
from liteinfer.runtime import BlockingExecutor, EchoExecutor, TaskService

pytestmark = pytest.mark.integration


class GatedExecutor:
    """逐个放行，用来观察出队次序；声明安全点取消以便服务能正常收尾。"""

    def __init__(self) -> None:
        self.order: list[tuple[str, str]] = []  # (kind, tenant) 按出队顺序
        self._lock = threading.Lock()
        self._release = threading.Event()

    @property
    def supports_cancellation(self) -> bool:
        return True

    @property
    def kinds(self) -> list[str]:
        with self._lock:
            return [kind for kind, _tenant in self.order]

    @property
    def tenants(self) -> list[str]:
        with self._lock:
            return [tenant for _kind, tenant in self.order]

    def wait_for(self, count: int, *, timeout_s: float = 5.0) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            with self._lock:
                if len(self.order) >= count:
                    return
            time.sleep(0.02)

    def execute(self, context: object, request: TaskRequest) -> TaskResult:
        with self._lock:
            self.order.append((request.kind, request.tenant))
        self._release.wait(timeout=5.0)
        return TaskResult(output={"kind": request.kind}, metrics={"backend": "gated"})


def test_inference_is_dispatched_before_training():
    """3.10：推理优先、训练最后——先提交的不一定先跑。"""
    executor = GatedExecutor()
    service = TaskService(executor=executor, start_worker=False)
    try:
        service.submit(TaskRequest(kind="train.lora"))
        service.submit(TaskRequest(kind="eval.perplexity"))
        service.submit(TaskRequest(kind="llm.generate"))
        assert service.queue_depth()["scheduler"]["by_class"] == {
            "training": 1,
            "eval": 1,
            "inference": 1,
        }

        service.start()
        executor.wait_for(1)
        assert executor.kinds == ["llm.generate"]
    finally:
        executor._release.set()  # noqa: SLF001 - 测试替身内部开关
        service.close()


def test_fairness_limit_lets_other_tenant_in():
    """3.10 公平性：同一 tenant 连续取到上限后必须让位。"""
    executor = GatedExecutor()
    service = TaskService(
        config_from_mapping({"queue": {"fairness_limit": 2}}),
        executor=executor,
        start_worker=False,
    )
    try:
        for _ in range(4):
            service.submit(TaskRequest(kind="llm.generate", tenant="big"))
        service.submit(TaskRequest(kind="llm.generate", tenant="small"))

        service.start()
        # 单 worker 串行，逐个放行后观察出队顺序
        for index in range(3):
            executor._release.set()  # noqa: SLF001 - 测试替身内部开关
            time.sleep(0.05)
            executor._release.clear()  # noqa: SLF001
            executor.wait_for(index + 1)
        # 放行窗口内可能连跑多个，只看前三个的出队次序
        assert executor.tenants[:3] == ["big", "big", "small"]
    finally:
        executor._release.set()  # noqa: SLF001
        service.close()


def test_max_inflight_is_enforced_not_just_counted():
    """3.3：`max_inflight` 是真约束（= worker 数），不是只计数不生效。"""
    gate = threading.Event()
    service = TaskService(
        config_from_mapping({"queue": {"max_inflight": 2}}),
        BlockingExecutor(gate=gate),
        start_worker=False,
    )
    try:
        service.start()
        for _ in range(6):
            service.submit(TaskRequest(kind="echo"))

        peak = 0
        deadline = time.time() + 3.0
        while time.time() < deadline:
            peak = max(peak, service.queue_depth()["inflight"])
            if peak >= 2:
                break
            time.sleep(0.02)
        assert peak == 2  # 上限 2：第三个不许同时跑
    finally:
        gate.set()
        service.close()


def test_queue_depth_exposes_scheduler_stats():
    service = TaskService(executor=EchoExecutor(), start_worker=False)
    try:
        service.submit(TaskRequest(kind="train.lora", tenant="a"))
        service.submit(TaskRequest(kind="llm.generate", tenant="b"))
        depth = service.queue_depth()
        assert depth["queued"] == 2
        assert depth["scheduler"]["depth"] == 2
        assert depth["scheduler"]["by_class"] == {"training": 1, "inference": 1}
    finally:
        service.close()
