"""结构化日志与指标测试（WBS-1.7）。

要求：请求 ID 与任务 ID 必须进入每条日志，且日志可被机器解析。
"""

from __future__ import annotations

import io
import json
import logging

import pytest

from liteinfer.core import LiteInferConfig, config_from_mapping
from liteinfer.observability import (
    JsonFormatter,
    MetricRegistry,
    bind,
    configure_logging,
    current_context,
    get_logger,
    log_event,
    redact,
)

pytestmark = pytest.mark.unit


def test_json_formatter_includes_context_ids():
    logger = get_logger("test.formatter")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    with bind(request_id="req-1", task_id="task-1"):
        log_event(logger, "task.submitted", kind="echo")
    handler.flush()

    payload = json.loads(stream.getvalue().strip().splitlines()[0])
    assert payload["request_id"] == "req-1"
    assert payload["task_id"] == "task-1"
    assert payload["message"] == "task.submitted"
    assert payload["fields"]["event"] == "task.submitted"
    assert payload["fields"]["kind"] == "echo"
    logger.handlers = []


def test_context_is_restored_after_bind():
    with bind(request_id="req-2"):
        assert current_context()["request_id"] == "req-2"
    assert current_context()["request_id"] is None


def test_configure_logging_uses_json_by_default():
    config: LiteInferConfig = config_from_mapping({})
    logger = configure_logging(level=config.observability.level, json_logs=True)
    assert logger.handlers
    assert isinstance(logger.handlers[0].formatter, JsonFormatter)
    assert logger.propagate is False


def test_redact_hides_sensitive_keys():
    payload = redact({"prompt": "hello", "api_key": "secret"}, keys=frozenset({"api_key"}))
    assert payload["api_key"] == "***"
    assert payload["prompt"] == "hello"


def test_metrics_snapshot_is_json_serializable():
    registry = MetricRegistry()
    registry.counter("tasks.submitted").inc()
    registry.gauge("queue.depth").set(3)
    for value in range(1, 101):
        registry.samples("latency_ms").observe(float(value))

    snapshot = registry.snapshot()
    assert snapshot["counters"]["tasks.submitted"] == 1.0
    assert snapshot["gauges"]["queue.depth"] == 3.0
    assert snapshot["samples"]["latency_ms"]["count"] == 100
    assert snapshot["samples"]["latency_ms"]["p95"] == 95.0
    json.dumps(snapshot)
