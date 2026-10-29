"""WBS-2 端到端：注册 → 启动前门禁 → 调用 → 结果（走真实 HTTP 服务）。

门禁用例用 `missing_modules` 夹具显式声明「依赖不存在」，不依赖本机真实安装状态：
宿主机装了 torch 之后，原先「本机肯定没有 torch」的断言会直接失效。
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from liteinfer.adapters import DependencySpec, describe_capabilities
from liteinfer.api import HttpService
from liteinfer.bootstrap import build_service
from liteinfer.core import ErrorCode

pytestmark = pytest.mark.integration


class _FakeLLMAdapter:
    """测试替身：满足协议但**不提供真实推理**，只用于验证门禁行为。"""

    adapter_id = "test.llm"
    api_version = "1.0.0"
    framework_api_range = ">=1.0.0,<2.0.0"

    def validate_config(self, config: Any) -> None:
        return None

    def load(self, context: Any) -> None:
        return None

    def infer(self, request: Any) -> Any:
        return {"output": "fake"}

    def release(self) -> None:
        return None


def _post(url: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(  # noqa: S310 - 本地测试端口
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get(url: str) -> tuple[int, dict[str, Any]]:
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310
        return response.status, json.loads(response.read().decode("utf-8"))


@pytest.fixture()
def server_url():
    service = build_service()
    http = HttpService(service, port=0)
    url = http.start()
    try:
        yield url
    finally:
        http.stop()
        service.close()


def test_models_endpoint_lists_builtin_adapter(server_url: str):
    _, payload = _get(f"{server_url}/v1/models")
    ids = [item["adapter_id"] for item in payload["models"]]
    assert "builtin.weights-verify" in ids
    entry = next(
        item for item in payload["models"] if item["adapter_id"] == "builtin.weights-verify"
    )
    assert entry["ready"] is True
    assert entry["kinds"] == ["weights.verify"]


def test_weights_verify_runs_end_to_end(server_url: str, tmp_path: Path):
    content = b"real-weights-bytes"
    (tmp_path / "model.safetensors").write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()

    status, payload = _post(
        f"{server_url}/v1/tasks",
        {
            "kind": "weights.verify",
            "payload": {
                "model_id": "demo",
                "root": str(tmp_path),
                "weights": [
                    {
                        "source": "model.safetensors",
                        "format": "safetensors",
                        "sha256": digest,
                        "size_bytes": len(content),
                    }
                ],
            },
        },
    )
    assert status == 201
    task_id = payload["task"]["task_id"]

    deadline_url = f"{server_url}/v1/tasks/{task_id}"
    for _ in range(50):
        _, payload = _get(deadline_url)
        record = payload["task"]
        if record["status"] in ("succeeded", "failed"):
            break
    assert record["status"] == "succeeded"
    assert record["result"]["output"]["ok"] is True


def test_corrupted_weights_are_reported_not_accepted(server_url: str, tmp_path: Path):
    """G2 前置：损坏权重不能被误判为有效模型。"""
    (tmp_path / "model.safetensors").write_bytes(b"tampered-bytes")
    status, payload = _post(
        f"{server_url}/v1/tasks",
        {
            "kind": "weights.verify",
            "payload": {
                "model_id": "demo",
                "root": str(tmp_path),
                "weights": [
                    {
                        "source": "model.safetensors",
                        "format": "safetensors",
                        "sha256": "00" * 32,
                        "size_bytes": 14,
                    }
                ],
            },
        },
    )
    assert status == 201
    task_id = payload["task"]["task_id"]
    for _ in range(50):
        _, payload = _get(f"{server_url}/v1/tasks/{task_id}")
        record = payload["task"]
        if record["status"] in ("succeeded", "failed"):
            break
    # 校验任务本身成功返回，但报告必须明确不通过
    assert record["status"] == "succeeded"
    output = record["result"]["output"]
    assert output["ok"] is False
    assert output["checks"][0]["status"] == "hash_mismatch"


def test_missing_dependency_is_rejected_before_execution(
    missing_modules: Callable[..., None],
) -> None:
    """门禁是否生效，不该由本机装了什么决定——这里显式隐藏 torch / sglang。"""
    missing_modules("torch", "sglang")
    service = build_service(
        configure=lambda registry: registry.register(
            _FakeLLMAdapter(),
            capability=describe_capabilities(kinds=("llm.generate",)),
            dependency=DependencySpec(modules=("torch", "sglang"), level="L1"),
        )
    )
    http = HttpService(service, port=0)
    url = http.start()
    try:
        status, payload = _post(f"{url}/v1/tasks", {"kind": "llm.generate"})
        assert status == 503
        assert payload["error"]["code"] == ErrorCode.DEPENDENCY_MISSING.value
        assert payload["error"]["retryable"] is False
        assert "torch" in payload["error"]["details"]["missing"]
    finally:
        http.stop()
        service.close()


def test_unknown_kind_still_reports_unsupported(server_url: str):
    status, payload = _post(f"{server_url}/v1/tasks", {"kind": "no.such.kind"})
    assert status == 201  # 提交成功，执行阶段才判定未接入
    task_id = payload["task"]["task_id"]
    for _ in range(50):
        _, payload = _get(f"{server_url}/v1/tasks/{task_id}")
        record = payload["task"]
        if record["status"] in ("succeeded", "failed"):
            break
    assert record["status"] == "failed"
    assert record["error"]["code"] == ErrorCode.UNSUPPORTED.value
