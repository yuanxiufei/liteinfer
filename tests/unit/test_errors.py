"""错误码契约测试（WBS-1.2）。"""

from __future__ import annotations

import pytest

from liteinfer.core import (
    DEFAULT_HTTP_STATUS,
    ErrorCode,
    LiteInferError,
    invalid_argument,
    queue_full,
)

pytestmark = pytest.mark.unit


def test_http_status_mapping_is_complete():
    assert set(DEFAULT_HTTP_STATUS) == set(ErrorCode)


def test_queue_full_maps_to_429_and_is_retryable():
    error = queue_full("队列已满", limit=10)
    assert error.http_status == 429
    assert error.retryable is True
    assert error.to_dict()["code"] == "queue_full"


def test_validation_error_is_not_retryable():
    error = invalid_argument("kind 不能为空")
    assert error.http_status == 400
    assert error.retryable is False


def test_custom_retryable_overrides_default():
    error = LiteInferError(ErrorCode.INVALID_ARGUMENT, "x", retryable=True)
    assert error.retryable is True


def test_message_includes_code():
    assert str(invalid_argument("bad")).startswith("[invalid_argument]")
