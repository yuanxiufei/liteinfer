"""LiteInfer — 通用训推基础设施。

骨架期已落地：核心领域模型、任务状态机、SDK、CLI、独立 HTTP 服务、
结构化日志与指标。真实后端（SGLang / 训练执行路径）在 WBS-2 / WBS-5 接入，
本包在此之前不提供任何推理能力的证据。
"""

from .client import LiteInfer, session
from .core import (
    ErrorCode,
    LiteInferConfig,
    LiteInferError,
    TaskRecord,
    TaskRequest,
    TaskStatus,
)
from .runtime import TaskService

__version__ = "0.1.0"

__all__ = [
    "ErrorCode",
    "LiteInfer",
    "LiteInferConfig",
    "LiteInferError",
    "TaskRecord",
    "TaskRequest",
    "TaskService",
    "TaskStatus",
    "__version__",
    "session",
]
