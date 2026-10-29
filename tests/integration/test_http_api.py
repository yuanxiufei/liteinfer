"""HTTP 服务契约测试（WBS-1.6）。

重点验证：错误码到 HTTP 状态码的映射，以及「拒绝时不把任务塞进后台」。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

import pytest

from liteinfer.api import HttpService
from liteinfer.core import LiteInferConfig, TaskRequest, TaskStatus, config_from_mapping
from liteinfer.runtime import ExecutionContext, FunctionExecutor, TaskService

pytestmark = pytest.mark.integration


def _slow_echo(context: ExecutionContext, request: TaskRequest) -> dict[str, Any]:
    """停留 0.5s 并在循环中检查取消，让「运行中取消」这条路径可测。"""
    deadline = time.perf_counter() + 0.5
    while time.perf_counter() < deadline:
        context.check_cancel()
        time.sleep(0.01)
    return {"output": {"echo": dict(request.payload)}, "metrics": {"backend": "echo"}}


@pytest.fixture()
def http_service():
    service = TaskService(LiteInferConfig(), FunctionExecutor({"echo": _slow_echo}))
    http = HttpService(service, port=0)
    url = http.start()
    try:
        yield url, service
    finally:
        http.stop()
        service.close()


def _get(url: str) -> tuple[int, dict]:
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310
        return response.status, json.loads(response.read().decode("utf-8"))


def _post(url: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body or {}).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310
        url, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return response.status, json.loads(response.read().decode("utf-8"))


def _post_error(url: str, body: dict | None = None) -> tuple[int, dict]:
    try:
        return _post(url, body)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_error(url: str) -> tuple[int, dict]:
    try:
        return _get(url)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_healthz(http_service):
    url, _ = http_service
    status, payload = _get(f"{url}/healthz")
    assert status == 200
    assert payload["status"] == "ok"
    assert "queue" in payload


def test_submit_and_query_task(http_service):
    url, _ = http_service
    status, payload = _post(f"{url}/v1/tasks", {"kind": "echo", "payload": {"a": 1}})
    assert status == 201
    task_id = payload["task"]["task_id"]

    status, payload = _get(f"{url}/v1/tasks/{task_id}")
    assert status == 200
    assert payload["task"]["task_id"] == task_id


def test_unknown_task_returns_404(http_service):
    url, _ = http_service
    status, payload = _get_error(f"{url}/v1/tasks/does-not-exist")
    assert status == 404
    assert payload["error"]["code"] == "not_found"


def test_invalid_body_returns_400(http_service):
    url, _ = http_service
    status, payload = _post_error(f"{url}/v1/tasks", {"payload": {}})
    assert status == 400
    assert payload["error"]["code"] == "invalid_argument"


def test_unknown_kind_returns_task_failure_not_500(http_service):
    url, service = http_service
    assert _post(f"{url}/v1/tasks", {"kind": "unknown-kind"})[0] == 201
    finished = service.wait(service.list_tasks(kind="unknown-kind")[0].task_id, timeout_s=5.0)
    assert finished.status is TaskStatus.FAILED
    assert finished.error is not None and finished.error.code == "unsupported"


def test_backpressure_returns_429_without_buffering():
    config = config_from_mapping({"queue": {"max_queued": 1}})
    service = TaskService(config, start_worker=False)
    http = HttpService(service, port=0)
    url = http.start()
    try:
        assert _post(f"{url}/v1/tasks", {"kind": "echo"})[0] == 201
        status, payload = _post_error(f"{url}/v1/tasks", {"kind": "echo"})
        assert status == 429
        assert payload["error"]["code"] == "queue_full"
        assert payload["error"]["retryable"] is True
        assert len(service.list_tasks()) == 1  # 拒绝的任务没有进后台
    finally:
        http.stop()
        service.close()


def test_preempt_endpoint_refuses_when_executor_declares_no_safe_point(http_service):
    """HTTP 层不做业务判断：未声明安全点的执行器，抢占由服务层报 409。"""
    url, _ = http_service
    _, payload = _post(f"{url}/v1/tasks", {"kind": "echo", "payload": {"text": "hi"}})
    task_id = payload["task"]["task_id"]

    status, payload = _post_error(f"{url}/v1/tasks/{task_id}/preempt")
    assert status == 409
    assert payload["error"]["code"] == "conflict"
    assert "安全点" in payload["error"]["message"]


def test_unknown_task_action_returns_405(http_service):
    url, _ = http_service
    _, payload = _post(f"{url}/v1/tasks", {"kind": "echo"})
    task_id = payload["task"]["task_id"]

    status, payload = _post_error(f"{url}/v1/tasks/{task_id}/teleport")
    assert status == 405
    assert payload["error"]["code"] == "unsupported"


def test_cancel_endpoint_returns_202_and_reports_two_phase_ack(http_service):
    url, _ = http_service
    _, payload = _post(f"{url}/v1/tasks", {"kind": "echo", "payload": {"text": "hi"}})
    task_id = payload["task"]["task_id"]

    status, payload = _post(f"{url}/v1/tasks/{task_id}/cancel")
    assert status == 202
    assert payload["cancel"]["accepted"] is True
    assert payload["cancel"]["status"] in ("cancelled", "cancelling")


def test_task_list_and_metrics(http_service):
    url, _ = http_service
    _post(f"{url}/v1/tasks", {"kind": "echo", "tenant": "team-a"})
    status, payload = _get(f"{url}/v1/tasks?tenant=team-a")
    assert status == 200
    assert payload["tasks"]
    status, payload = _get(f"{url}/v1/metrics")
    assert status == 200
    assert payload["metrics"]["counters"]["tasks.submitted"] >= 1.0


def test_unknown_route_returns_404(http_service):
    url, _ = http_service
    status, _ = _get_error(f"{url}/v1/nope")
    assert status == 404


def test_wrong_method_returns_405(http_service):
    url, _ = http_service
    status, payload = _post_error(f"{url}/healthz")
    assert status == 405
    assert payload["error"]["code"] == "unsupported"


def test_models_endpoint_lists_registered_adapters(http_service):
    """WBS-2 接入后：`/v1/models` 必须如实反映注册中心，而不是恒为空。"""
    url, _ = http_service
    status, payload = _get(f"{url}/v1/models")
    assert status == 200
    # 该 fixture 用的是裸 TaskService（未注入注册中心），列表为空是"尚未注册"而非"能力缺失"
    assert payload["models"] == []
    assert "尚未注册" in payload["note"]
