"""任务生命周期、资源租约与执行接口（WBS-1 / WBS-3）。

租约是**逻辑配额**，不等于 GPU 显存硬隔离；取消只开放给声明安全点的执行器。
"""

from .executor import (
    BlockingExecutor,
    EchoExecutor,
    ExecutionContext,
    Executor,
    FunctionExecutor,
    default_executor,
)
from .lease import (
    CallbackMeter,
    LeaseManager,
    ResourceLease,
    ResourceMeter,
    ResourceSample,
)
from .ports import AdapterInfo, AdapterLookup
from .scheduler import CLASS_ORDER, TaskQueue, classify_kind
from .service import CancelAck, TaskService

__all__ = [
    "CLASS_ORDER",
    "AdapterInfo",
    "AdapterLookup",
    "BlockingExecutor",
    "CallbackMeter",
    "CancelAck",
    "EchoExecutor",
    "ExecutionContext",
    "Executor",
    "FunctionExecutor",
    "LeaseManager",
    "ResourceLease",
    "ResourceMeter",
    "ResourceSample",
    "TaskQueue",
    "TaskService",
    "classify_kind",
    "default_executor",
]
