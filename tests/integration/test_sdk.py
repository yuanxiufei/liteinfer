"""SDK 集成测试（WBS-1.4）。

要求：不启动 HTTP 服务也能完成提交、查询、取消与取结果。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from liteinfer import LiteInfer
from liteinfer.api import HttpService
from liteinfer.core import ErrorCode, LiteInferConfig, LiteInferError, TaskStatus
from liteinfer.runtime import EchoExecutor, TaskService

pytestmark = pytest.mark.integration


def test_sdk_submit_wait_and_result():
    with LiteInfer(executor=EchoExecutor()) as client:
        task_id = client.submit("echo", payload={"text": "hi"}, client_token="sdk-1")
        record = client.wait(task_id, timeout_s=5.0)
        assert record.status is TaskStatus.SUCCEEDED
        assert client.result(task_id)["echo"] == {"text": "hi"}


def test_sdk_result_on_failed_task_raises_conflict():
    with LiteInfer() as client:  # 默认执行器只认 echo
        task_id = client.submit("llm.generate")
        client.wait(task_id, timeout_s=5.0)
        with pytest.raises(LiteInferError) as excinfo:
            client.result(task_id)
        assert excinfo.value.code is ErrorCode.CONFLICT


def test_sdk_cancel_queued_task():
    config = {"queue": {"max_queued": 8}}
    with LiteInfer(config=config, executor=EchoExecutor(sleep_s=5.0)) as client:
        task_id = client.submit("echo")
        ack = client.cancel(task_id)
        assert ack.accepted
        assert client.status(task_id).status in (TaskStatus.CANCELLED, TaskStatus.CANCELLING)


def test_sdk_still_works_after_http_service_is_stopped():
    """G1 退出条件：HTTP 服务停止不影响独立 SDK 的基本功能。

    两条都要验：服务停掉后确实连不上；且此时 SDK 仍可提交、等待、取结果。
    """
    service = TaskService(LiteInferConfig(), EchoExecutor())
    http = HttpService(service, port=0)
    url = http.start()

    request = urllib.request.Request(  # noqa: S310 - 本地测试端口
        f"{url}/v1/tasks",
        data=json.dumps({"kind": "echo"}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        assert response.status == 201

    http.stop()
    service.close()

    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(f"{url}/healthz", timeout=2)  # noqa: S310

    with LiteInfer(executor=EchoExecutor()) as client:
        task_id = client.submit("echo", payload={"text": "after-stop"})
        record = client.wait(task_id, timeout_s=5.0)
        assert record.status is TaskStatus.SUCCEEDED
        assert client.result(task_id)["echo"] == {"text": "after-stop"}


def test_sdk_lists_and_metrics():
    with LiteInfer(executor=EchoExecutor()) as client:
        client.submit("echo", tenant="team-a")
        assert len(client.tasks(tenant="team-a")) == 1
        assert client.metrics()["counters"]["tasks.submitted"] == 1.0
