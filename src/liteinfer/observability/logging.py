"""结构化日志与上下文关联（WBS-1.7）。

要求：每个请求与任务都能通过 request_id / task_id 串起来；
日志既是给人看的，也要能被机器解析（JSON 行格式）。
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any

LOGGER_NAME = "liteinfer"

_request_id: ContextVar[str | None] = ContextVar("liteinfer_request_id", default=None)
_task_id: ContextVar[str | None] = ContextVar("liteinfer_task_id", default=None)

_RESERVED = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    """单行 JSON 日志。未知字段平铺进 `fields`，不丢信息。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": _request_id.get(),
            "task_id": _task_id.get(),
        }
        fields = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED and not key.startswith("_")
        }
        if fields:
            payload["fields"] = fields
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """人读格式：把上下文 ID 直接拼进行首，便于 grep。"""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        request_id = _request_id.get()
        task_id = _task_id.get()
        prefix = ""
        if request_id or task_id:
            prefix = f"[{request_id or '-'}/{task_id or '-'}] "
        return f"{prefix}{base}"


def configure_logging(
    *,
    level: str = "INFO",
    json_logs: bool = True,
    log_file: str | None = None,
    force: bool = False,
) -> logging.Logger:
    """配置 `liteinfer` 命名空间的 logger。

    只改自己的 logger，不劫持 root——把全局配置留给调用方。
    """
    logger = logging.getLogger(LOGGER_NAME)
    formatter: logging.Formatter = JsonFormatter() if json_logs else TextFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(formatter)
    logger.handlers = handlers
    logger.setLevel(level.upper())
    logger.propagate = False
    if force:
        logging.getLogger(LOGGER_NAME).disabled = False
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(LOGGER_NAME if name is None else f"{LOGGER_NAME}.{name}")


@contextmanager
def bind(**context: str | None) -> Iterator[None]:
    """绑定上下文变量（request_id / task_id）。"""
    tokens: list[Token[str | None]] = []
    if "request_id" in context:
        tokens.append(_request_id.set(context["request_id"]))
    if "task_id" in context:
        tokens.append(_task_id.set(context["task_id"]))
    try:
        yield
    finally:
        for token in reversed(tokens):
            token.var.reset(token)


def current_context() -> dict[str, str | None]:
    return {"request_id": _request_id.get(), "task_id": _task_id.get()}


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """事件式日志：事件名固定为 `event`，其余字段平铺，便于聚合。"""
    extra: dict[str, Any] = {"event": event}
    for key, value in fields.items():
        if key in _RESERVED:
            extra[f"field_{key}"] = value
        else:
            extra[key] = value
    logger.log(level, event, extra=extra)


def redact(payload: Mapping[str, Any], *, keys: frozenset[str] = frozenset()) -> dict[str, Any]:
    """日志脱敏：命中键名的值整段替换为 `***`。"""
    return {key: ("***" if key in keys else value) for key, value in payload.items()}
