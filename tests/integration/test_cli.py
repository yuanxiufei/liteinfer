"""CLI 验收（WBS-1.5）。

CLI 与 HTTP 服务调用同一套核心领域服务：这里用真实起服务的方式验证，
不靠 mock 假装命令可用。
"""

from __future__ import annotations

import json

import pytest

from liteinfer.api import HttpService
from liteinfer.cli import main
from liteinfer.core import LiteInferConfig
from liteinfer.runtime import TaskService, default_executor

pytestmark = pytest.mark.integration


@pytest.fixture()
def server_url():
    service = TaskService(LiteInferConfig(), default_executor())
    http = HttpService(service, port=0)
    url = http.start()
    try:
        yield url
    finally:
        http.stop()
        service.close()


def test_doctor_runs_without_gpu_claims(capsys):
    assert main(["doctor"]) == 0
    output = capsys.readouterr().out
    assert "LiteInfer doctor" in output
    assert "默认配置校验通过" in output


def test_model_list_against_server(server_url, capsys):
    assert main(["model", "list", "--url", server_url]) == 0
    assert "无已注册适配器" in capsys.readouterr().out


def test_infer_and_task_status_round_trip(server_url, capsys):
    assert main(["infer", "--kind", "echo", "--payload", '{"text":"hi"}', "--url", server_url]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "succeeded"

    task_id = payload["task_id"]
    assert main(["task", "status", task_id, "--url", server_url]) == 0
    assert task_id in capsys.readouterr().out


def test_task_list_command(server_url):
    assert main(["infer", "--kind", "echo", "--url", server_url]) == 0
    assert main(["task", "list", "--url", server_url]) == 0


def test_task_cancel_command(server_url, capsys):
    assert main(["infer", "--kind", "echo", "--url", server_url]) == 0
    task_id = json.loads(capsys.readouterr().out)["task_id"]

    # 任务已终结时取消应报冲突，而不是静默成功
    assert main(["task", "cancel", task_id, "--url", server_url]) == 1


def test_unreachable_server_reports_readable_error():
    assert main(["task", "list", "--url", "http://127.0.0.1:1"]) == 1
