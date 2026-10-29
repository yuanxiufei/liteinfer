"""SDK 集成测试（WBS-1.4）。

要求：不启动 HTTP 服务也能完成提交、查询、取消与取结果。
"""

from __future__ import annotations

import pytest

from liteinfer import LiteInfer
from liteinfer.core import ErrorCode, LiteInferError, TaskStatus
from liteinfer.runtime import EchoExecutor

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


def test_sdk_lists_and_metrics():
    with LiteInfer(executor=EchoExecutor()) as client:
        client.submit("echo", tenant="team-a")
        assert len(client.tasks(tenant="team-a")) == 1
        assert client.metrics()["counters"]["tasks.submitted"] == 1.0
