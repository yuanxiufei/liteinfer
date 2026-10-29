"""进程内 SDK（WBS-1.4）。

要求：不依赖 HTTP 服务即可提交与查询任务；HTTP 停掉不影响本 SDK 的基本功能。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from .core import (
    LiteInferConfig,
    ModelRef,
    ResourceBudget,
    TaskRecord,
    TaskRequest,
    TaskStatus,
    config_from_mapping,
)
from .observability import configure_logging
from .runtime import CancelAck, Executor, LeaseManager, TaskService


class LiteInfer:
    """SDK 入口。上下文管理器保证退出时回收资源并关闭工作线程。"""

    def __init__(
        self,
        config: LiteInferConfig | Mapping[str, Any] | None = None,
        *,
        executor: Executor | None = None,
        lease_manager: LeaseManager | None = None,
        configure_logs: bool = True,
    ) -> None:
        if isinstance(config, Mapping):
            config = config_from_mapping(config)
        self._config = config or LiteInferConfig()
        self._config.validate()
        if configure_logs:
            configure_logging(
                level=self._config.observability.level,
                json_logs=self._config.observability.json_logs,
                log_file=self._config.observability.log_file,
            )
        self._service = TaskService(
            self._config, executor=executor, lease_manager=lease_manager
        )

    @property
    def service(self) -> TaskService:
        return self._service

    @property
    def config(self) -> LiteInferConfig:
        return self._config

    def submit(
        self,
        kind: str,
        *,
        payload: Mapping[str, Any] | None = None,
        model: ModelRef | None = None,
        budget: ResourceBudget | None = None,
        priority: int = 0,
        tenant: str = "default",
        client_token: str | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> str:
        request = TaskRequest(
            kind=kind,
            payload=dict(payload or {}),
            model=model,
            budget=budget or ResourceBudget(),
            priority=priority,
            tenant=tenant,
            client_token=client_token,
            labels=dict(labels or {}),
        )
        return self._service.submit(request).task_id

    def submit_request(self, request: TaskRequest) -> str:
        return self._service.submit(request).task_id

    def status(self, task_id: str) -> TaskRecord:
        return self._service.get(task_id)

    def wait(self, task_id: str, *, timeout_s: float | None = None) -> TaskRecord:
        return self._service.wait(task_id, timeout_s=timeout_s)

    def result(self, task_id: str, *, timeout_s: float | None = None) -> Any:
        record = self.wait(task_id, timeout_s=timeout_s)
        if record.status is not TaskStatus.SUCCEEDED:
            from .core import conflict

            raise conflict(
                "任务未成功，不能取结果",
                task_id=task_id,
                status=record.status.value,
                error=record.error.to_dict() if record.error else None,
            )
        return record.result.output if record.result else None

    def cancel(self, task_id: str) -> CancelAck:
        return self._service.cancel(task_id)

    def tasks(
        self,
        *,
        status: TaskStatus | None = None,
        tenant: str | None = None,
        kind: str | None = None,
    ) -> list[TaskRecord]:
        return self._service.list_tasks(status=status, tenant=tenant, kind=kind)

    def metrics(self) -> dict[str, Any]:
        return self._service.metrics()

    def close(self) -> None:
        self._service.close()

    def __enter__(self) -> LiteInfer:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def session(
    config: LiteInferConfig | Mapping[str, Any] | None = None, **kwargs: Any
) -> Iterator[LiteInfer]:
    """便捷入口：`with liteinfer.session() as li: ...`。"""
    client = LiteInfer(config, **kwargs)
    try:
        yield client
    finally:
        client.close()
