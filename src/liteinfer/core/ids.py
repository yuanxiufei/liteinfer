"""标识符生成（WBS-1.2）。

每个任务、请求与租约都必须有可追踪的唯一 ID；ID 只保证唯一与可读，
不含业务语义，禁止用自增序号（跨进程会撞）。
"""

from __future__ import annotations

import uuid

TASK_PREFIX = "task"
REQUEST_PREFIX = "req"
LEASE_PREFIX = "lease"


def new_id(prefix: str) -> str:
    if not prefix:
        raise ValueError("prefix 不能为空")
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def new_task_id() -> str:
    return new_id(TASK_PREFIX)


def new_request_id() -> str:
    return new_id(REQUEST_PREFIX)


def new_lease_id() -> str:
    return new_id(LEASE_PREFIX)
