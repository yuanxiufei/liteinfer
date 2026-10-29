"""任务生命周期、资源租约与执行接口（WBS-1 / WBS-3）。

租约是**逻辑配额**，不等于 GPU 显存硬隔离；取消只开放给声明安全点的执行器。
"""

from .cache import KVCacheBudget, KVCacheStats, TensorCacheStats, TensorFeatureCache
from .executor import (
    BlockingExecutor,
    CheckpointExecutor,
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
from .meter import (
    BestEffortMeter,
    CompositeMeter,
    DiskMeter,
    NvmlMeter,
    ProcMeter,
    build_meter,
    parse_smi_memory,
)
from .ports import AdapterInfo, AdapterLookup
from .preemption import PreemptCandidate, PreemptionPlanner, rounds_of
from .scheduler import CLASS_ORDER, TaskQueue, classify_kind
from .service import CancelAck, PreemptAck, TaskService

__all__ = [
    "CLASS_ORDER",
    "AdapterInfo",
    "AdapterLookup",
    "BestEffortMeter",
    "BlockingExecutor",
    "CallbackMeter",
    "CancelAck",
    "CheckpointExecutor",
    "CompositeMeter",
    "DiskMeter",
    "EchoExecutor",
    "ExecutionContext",
    "Executor",
    "FunctionExecutor",
    "KVCacheBudget",
    "KVCacheStats",
    "LeaseManager",
    "NvmlMeter",
    "PreemptAck",
    "PreemptCandidate",
    "PreemptionPlanner",
    "ProcMeter",
    "ResourceLease",
    "ResourceMeter",
    "ResourceSample",
    "TaskQueue",
    "TaskService",
    "TensorCacheStats",
    "TensorFeatureCache",
    "build_meter",
    "classify_kind",
    "default_executor",
    "parse_smi_memory",
    "rounds_of",
]
