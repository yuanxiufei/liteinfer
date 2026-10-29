"""跨层稳定错误码与异常语义（WBS-1.2）。

纪律：错误码是契约的一部分。SDK / CLI / HTTP 服务共用同一套码，
任一层都不允许自己发明一个新的错误字符串；新增码必须同步更新
`DEFAULT_HTTP_STATUS`、`RETRYABLE` 与文档。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """LiteInfer 统一错误码（本身就是字符串，便于序列化与日志检索）。"""

    INVALID_ARGUMENT = "invalid_argument"
    UNSUPPORTED = "unsupported"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    QUEUE_FULL = "queue_full"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    #: 可选依赖缺失（ADR-0003 的能力分级）。不是请求错误，重试也不会自己装好依赖。
    DEPENDENCY_MISSING = "dependency_missing"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    BACKEND_ERROR = "backend_error"
    INTERNAL = "internal"


DEFAULT_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.INVALID_ARGUMENT: 400,
    ErrorCode.UNSUPPORTED: 400,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.QUEUE_FULL: 429,
    ErrorCode.RESOURCE_EXHAUSTED: 503,
    ErrorCode.DEPENDENCY_MISSING: 503,
    ErrorCode.CANCELLED: 409,
    ErrorCode.TIMEOUT: 504,
    ErrorCode.BACKEND_ERROR: 500,
    ErrorCode.INTERNAL: 500,
}

#: 允许客户端重试的码。其余码重试只会制造重复失败。
RETRYABLE: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.QUEUE_FULL,
        ErrorCode.RESOURCE_EXHAUSTED,
        ErrorCode.TIMEOUT,
        ErrorCode.BACKEND_ERROR,
        ErrorCode.INTERNAL,
    }
)


class LiteInferError(Exception):
    """所有对外错误的唯一出口。

    `http_status` 只是给 HTTP 传输层的建议值；领域层不得依赖 HTTP 语义。
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        retryable: bool | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: dict[str, Any] = dict(details or {})
        self.retryable = (code in RETRYABLE) if retryable is None else retryable
        self.http_status = http_status if http_status is not None else DEFAULT_HTTP_STATUS[code]

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "details": self.details,
            "retryable": self.retryable,
        }

    def __str__(self) -> str:
        return f"[{self.code.value}] {self.message}"


class TaskCancelled(LiteInferError):
    """执行器在安全点主动放弃执行时抛出。"""

    def __init__(self, message: str = "任务已取消", **details: Any) -> None:
        super().__init__(ErrorCode.CANCELLED, message, details=details, retryable=False)


def invalid_argument(message: str, **details: Any) -> LiteInferError:
    return LiteInferError(ErrorCode.INVALID_ARGUMENT, message, details=details)


def unsupported(message: str, **details: Any) -> LiteInferError:
    return LiteInferError(ErrorCode.UNSUPPORTED, message, details=details)


def not_found(message: str, **details: Any) -> LiteInferError:
    return LiteInferError(ErrorCode.NOT_FOUND, message, details=details)


def conflict(message: str, **details: Any) -> LiteInferError:
    return LiteInferError(ErrorCode.CONFLICT, message, details=details)


def queue_full(message: str, **details: Any) -> LiteInferError:
    return LiteInferError(ErrorCode.QUEUE_FULL, message, details=details)


def resource_exhausted(message: str, **details: Any) -> LiteInferError:
    return LiteInferError(ErrorCode.RESOURCE_EXHAUSTED, message, details=details)


def timed_out(message: str, **details: Any) -> LiteInferError:
    return LiteInferError(ErrorCode.TIMEOUT, message, details=details)


def dependency_missing(message: str, **details: Any) -> LiteInferError:
    """可选依赖缺失。默认不可重试：重试不会把依赖变出来。"""
    return LiteInferError(ErrorCode.DEPENDENCY_MISSING, message, details=details, retryable=False)


def backend_error(message: str, **details: Any) -> LiteInferError:
    return LiteInferError(ErrorCode.BACKEND_ERROR, message, details=details)
