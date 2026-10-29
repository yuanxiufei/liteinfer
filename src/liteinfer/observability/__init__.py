"""指标、结构化日志与追踪（WBS-1.7 / WBS-3）。

每个请求与任务必须可通过 request_id / task_id 关联；指标先落在进程内注册表，
外部导出在基线实测之后再加。
"""

from .logging import (
    JsonFormatter,
    TextFormatter,
    bind,
    configure_logging,
    current_context,
    get_logger,
    log_event,
    redact,
)
from .metrics import Counter, Gauge, MetricRegistry, Samples

__all__ = [
    "Counter",
    "Gauge",
    "JsonFormatter",
    "MetricRegistry",
    "Samples",
    "TextFormatter",
    "bind",
    "configure_logging",
    "current_context",
    "get_logger",
    "log_event",
    "redact",
]
