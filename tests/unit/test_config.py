"""配置校验测试（WBS-1.7）。

核心口径：未知配置项必须报错，不能被静默忽略（被忽略的配置比报错更危险）。
"""

from __future__ import annotations

import pytest

from liteinfer.core import ErrorCode, LiteInferConfig, LiteInferError, config_from_mapping

pytestmark = pytest.mark.unit


def test_default_config_is_valid():
    config = LiteInferConfig()
    assert config.problems() == []
    config.validate()


def test_from_mapping_accepts_partial_sections():
    config = config_from_mapping({"queue": {"max_queued": 8}})
    assert config.queue.max_queued == 8
    assert config.queue.max_queued_per_kind == 256  # 未给则用默认
    assert config.runtime.reclaim_on_terminal is True


def test_unknown_section_is_rejected():
    with pytest.raises(LiteInferError) as excinfo:
        config_from_mapping({"scheduler": {"x": 1}})
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT


def test_unknown_key_inside_section_is_rejected():
    with pytest.raises(LiteInferError) as excinfo:
        config_from_mapping({"queue": {"max_queue": 8}})
    assert "max_queue" in excinfo.value.details["unknown"][0]


def test_invalid_limit_is_rejected():
    with pytest.raises(LiteInferError) as excinfo:
        config_from_mapping({"queue": {"max_queued": 0}})
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT
    assert any("max_queued" in issue for issue in excinfo.value.details["issues"])


def test_invalid_log_level_is_rejected():
    with pytest.raises(LiteInferError):
        config_from_mapping({"observability": {"level": "VERBOSE"}})


def test_round_trip_dict():
    payload = config_from_mapping({"service_name": "svc"}).to_dict()
    assert payload["service_name"] == "svc"
    assert set(payload) == {"service_name", "queue", "runtime", "observability"}
