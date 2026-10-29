"""执行器协议与占位实现（WBS-1 / WBS-2 / WBS-5）。

执行器是「能力」的边界：真实后端（SGLang / PyTorch 训练循环）在 WBS-2 / WBS-5
按本协议接入，领域层只认协议，不认具体后端。

`supports_cancellation` 声明的是**安全点取消**能力：只有声明为 True 的执行器，
任务服务才会允许对运行中任务发起取消（WBS-3.9 的准入条件）。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..core import (
    LiteInferConfig,
    ResourceBudget,
    TaskCancelled,
    TaskRequest,
    TaskResult,
    unsupported,
)
from ..observability import get_logger

ExecuteFn = Callable[["ExecutionContext", TaskRequest], Any]


@dataclass
class ExecutionContext:
    """交给执行器的运行上下文。执行器只能读，不能改任务状态。"""

    task_id: str
    budget: ResourceBudget = field(default_factory=ResourceBudget)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    config: LiteInferConfig = field(default_factory=LiteInferConfig)
    logger: Any = None

    def __post_init__(self) -> None:
        if self.logger is None:
            self.logger = get_logger("executor")

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def check_cancel(self) -> None:
        """安全点检查。执行器应在可安全中止的位置主动调用。"""
        if self.cancelled:
            raise TaskCancelled("执行器在安全点检测到取消请求")


class Executor(Protocol):
    """执行器协议。"""

    @property
    def supports_cancellation(self) -> bool: ...

    def execute(self, context: ExecutionContext, request: TaskRequest) -> TaskResult: ...


class FunctionExecutor:
    """按 kind 分发的函数式执行器。

    用于骨架期与测试：真实能力未接入前，`infer` 之类命令不会假装有结果。
    """

    def __init__(
        self,
        handlers: Mapping[str, ExecuteFn],
        *,
        supports_cancellation: bool = True,
    ) -> None:
        self._handlers = dict(handlers)
        self._supports_cancellation = supports_cancellation

    @property
    def supports_cancellation(self) -> bool:
        return self._supports_cancellation

    def kinds(self) -> list[str]:
        return sorted(self._handlers)

    def execute(self, context: ExecutionContext, request: TaskRequest) -> TaskResult:
        handler = self._handlers.get(request.kind)
        if handler is None:
            raise unsupported(
                f"该 kind 尚未接入执行器：{request.kind}",
                kind=request.kind,
                supported=self.kinds(),
            )
        context.check_cancel()
        started = time.perf_counter()
        output = handler(context, request)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        metrics: dict[str, Any] = {"elapsed_ms": elapsed_ms}
        if isinstance(output, Mapping) and "metrics" in output:
            metrics.update(dict(output["metrics"]))
            output = output.get("output")
        return TaskResult(output=output, metrics=metrics)


class EchoExecutor:
    """最小执行器：回显 payload，用于冒烟与集成测试。

    它不加载任何模型，因此不能作为推理能力已具备的证据。
    """

    def __init__(self, *, sleep_s: float = 0.0) -> None:
        self._sleep_s = sleep_s

    @property
    def supports_cancellation(self) -> bool:
        return True

    def execute(self, context: ExecutionContext, request: TaskRequest) -> TaskResult:
        context.check_cancel()
        if self._sleep_s > 0:
            # 分段睡眠，保证取消请求能被及时感知（安全点）
            deadline = time.perf_counter() + self._sleep_s
            while time.perf_counter() < deadline:
                context.check_cancel()
                time.sleep(min(0.01, max(0.0, deadline - time.perf_counter())))
        context.check_cancel()
        return TaskResult(
            output={"echo": dict(request.payload), "kind": request.kind},
            metrics={"backend": "echo", "cancellable": True},
        )


class BlockingExecutor:
    """不支持安全点取消的执行器，用于验证「取消必须被拒绝」的口径。"""

    def __init__(self, *, gate: threading.Event | None = None) -> None:
        self._gate = gate or threading.Event()

    @property
    def supports_cancellation(self) -> bool:
        return False

    def execute(self, context: ExecutionContext, request: TaskRequest) -> TaskResult:
        self._gate.wait(timeout=30.0)
        return TaskResult(output={"kind": request.kind}, metrics={"backend": "blocking"})


def default_executor(
    extra_handlers: Mapping[str, ExecuteFn] | None = None,
) -> FunctionExecutor:
    """骨架期默认执行器。

    只注册 `echo`：它不加载任何模型，仅用于打通任务链路，
    **不能**作为「推理能力已具备」的证据。

    `extra_handlers` 供组合根（`bootstrap.py`）注入真实能力，
    避免 `runtime` 反向依赖适配器层（ADR-0003）。
    """

    def echo(context: ExecutionContext, request: TaskRequest) -> dict[str, Any]:
        context.check_cancel()
        return {
            "output": {"echo": dict(request.payload), "kind": request.kind},
            "metrics": {"backend": "echo"},
        }

    handlers: dict[str, ExecuteFn] = {"echo": echo}
    handlers.update(extra_handlers or {})
    return FunctionExecutor(handlers)
