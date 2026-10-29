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
    TaskPreempted,
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
    #: 抢占请求信号（WBS-3.9）。只有声明了 `supports_preemption` 的执行器会收到置位。
    preempt_event: threading.Event = field(default_factory=threading.Event)
    #: 恢复执行时带回的上一次安全点状态；首次执行为 `None`。
    resume_state: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.logger is None:
            self.logger = get_logger("executor")

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    @property
    def preempt_requested(self) -> bool:
        return self.preempt_event.is_set()

    def check_cancel(self) -> None:
        """安全点检查。执行器应在可安全中止的位置主动调用。"""
        if self.cancelled:
            raise TaskCancelled("执行器在安全点检测到取消请求")

    def checkpoint(self, state: Mapping[str, Any]) -> None:
        """在安全点交出可恢复状态（WBS-3.9）。

        语义只有一句：**执行器自己在安全点声明"从这儿能接着跑"**。
        服务端不解释、不补全、不猜测 state 的内容；收到抢占请求就抛
        `TaskPreempted` 把状态带出去。没走到安全点就没有任何状态可言。
        """
        if self.preempt_requested:
            raise TaskPreempted("执行器在安全点停下", state=state)


class Executor(Protocol):
    """执行器协议。"""

    @property
    def supports_cancellation(self) -> bool: ...

    @property
    def supports_preemption(self) -> bool:
        """是否声明了安全点抢占能力（WBS-3.9）。

        默认 False：**目前不对任何任务类型开放抢占**。
        只有实现了 `checkpoint()` 安全点并能从 `context.resume_state` 续跑的执行器
        才可以声明 True；声明了却不在安全点交状态，等于撒谎。
        """
        return False

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

    @property
    def supports_preemption(self) -> bool:
        return False

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

    @property
    def supports_preemption(self) -> bool:
        return False

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


class CheckpointExecutor:
    """分段执行 + 安全点保存，用于打通「抢占 → 恢复」链路（WBS-3.9）。

    它验证的是协议本身：**每步到安全点能交出状态、恢复后从下一步接着跑**。
    它不是任何真实后端，也不能当作「推理/训练任务已可抢占」的证据。
    """

    def __init__(self, *, steps: int = 20, step_sleep_s: float = 0.01) -> None:
        self._steps = steps
        self._step_sleep_s = step_sleep_s

    @property
    def supports_cancellation(self) -> bool:
        return True

    @property
    def supports_preemption(self) -> bool:
        return True

    def execute(self, context: ExecutionContext, request: TaskRequest) -> TaskResult:
        total = int(request.payload.get("steps", self._steps))
        done = int((context.resume_state or {}).get("done", 0))
        for index in range(done, total):
            if self._step_sleep_s > 0:
                time.sleep(self._step_sleep_s)
            context.check_cancel()
            # 安全点：已完成前 index 步，从这儿能接着跑
            context.checkpoint({"done": index})
        return TaskResult(
            output={"steps": total, "resumed_from": done},
            metrics={"backend": "checkpoint", "done": total},
        )


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
