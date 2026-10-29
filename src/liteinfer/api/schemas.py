"""HTTP 请求 / 响应契约（WBS-1.6）。

控制器只做协议转换：业务判断一律在 `TaskService` 里，不在路由里。
错误码沿用 `core.errors.ErrorCode`，HTTP 状态码只是它的映射结果。
"""

from __future__ import annotations

from typing import Any

from ..core import LiteInferError, TaskStatus
from ..core.models import TaskRecord
from ..runtime import CancelAck, PreemptAck


def task_response(record: TaskRecord) -> dict[str, Any]:
    return {"task": record.to_dict()}


def cancel_response(ack: CancelAck) -> dict[str, Any]:
    return {"cancel": ack.to_dict()}


def preempt_response(ack: PreemptAck) -> dict[str, Any]:
    """抢占回执。`accepted` / `completed` / `checkpoint` 分开报，不合成一个布尔。"""
    return {"preempt": ack.to_dict()}


def error_response(error: LiteInferError, *, request_id: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"error": error.to_dict()}
    if request_id:
        body["request_id"] = request_id
    return body


def health_response(*, service: str, version: str, queue_depth: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "ok",
        "service": service,
        "version": version,
        "queue": queue_depth,
    }


def parse_status(value: str | None) -> TaskStatus | None:
    """把查询参数映射成状态枚举；非法值抛 INVALID_ARGUMENT（400）。"""
    if value is None or value == "":
        return None
    try:
        return TaskStatus(value)
    except ValueError as exc:
        from ..core import invalid_argument

        raise invalid_argument(f"未知任务状态：{value}", status=value) from exc
