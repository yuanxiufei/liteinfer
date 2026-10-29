"""HTTP 请求与响应契约（WBS-1.6）。

控制器只做协议转换，业务逻辑留在 `TaskService`；
传输层在依赖锁定前使用标准库实现（见 ADR-0002）。
"""

from .http_server import CatalogFn, HttpService, LiteInferHTTPServer, run_server
from .schemas import (
    cancel_response,
    error_response,
    health_response,
    parse_status,
    task_response,
)

__all__ = [
    "CatalogFn",
    "HttpService",
    "LiteInferHTTPServer",
    "cancel_response",
    "error_response",
    "health_response",
    "parse_status",
    "run_server",
    "task_response",
]
