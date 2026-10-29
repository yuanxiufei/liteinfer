"""适配器注册中心与兼容性检查（WBS-2.2 / 2.3）。"""

from __future__ import annotations

from typing import Any

import pytest

from liteinfer.adapters import (
    DependencySpec,
    ModelRegistry,
    WeightVerifyAdapter,
    check_compatibility,
    describe_capabilities,
)
from liteinfer.core import ErrorCode, LiteInferError, conflict, unsupported

pytestmark = pytest.mark.unit


class _FakeAdapter:
    """测试替身：只满足协议，不提供任何真实能力（不得冒充 LLM 适配器）。"""

    adapter_id = "fake.llm"
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


def _register(registry: ModelRegistry, **kwargs: Any) -> None:
    capability = kwargs.pop(
        "capability", describe_capabilities(kinds=("llm.generate",), modalities=("text",))
    )
    registry.register(_FakeAdapter(), capability=capability, **kwargs)


def test_register_and_discover_by_kind():
    registry = ModelRegistry()
    _register(registry)
    assert registry.get("fake.llm") is not None
    assert registry.entry_for_kind("llm.generate").adapter_id == "fake.llm"
    assert registry.kinds() == ["llm.generate"]


def test_duplicate_adapter_id_is_rejected():
    registry = ModelRegistry()
    _register(registry)
    with pytest.raises(LiteInferError) as excinfo:
        _register(registry)
    assert excinfo.value.code is ErrorCode.CONFLICT


def test_incompatible_framework_api_is_rejected_at_registration():
    registry = ModelRegistry()

    class _TooNew(_FakeAdapter):
        framework_api_range = ">=2.0.0,<3.0.0"

    with pytest.raises(LiteInferError) as excinfo:
        registry.register(_TooNew(), capability=describe_capabilities(kinds=("llm.generate",)))
    assert excinfo.value.code is ErrorCode.UNSUPPORTED


def test_object_not_matching_protocol_is_rejected():
    registry = ModelRegistry()
    with pytest.raises(LiteInferError) as excinfo:
        registry.register(object())  # type: ignore[arg-type]
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT


def test_missing_dependency_registers_but_is_not_ready():
    registry = ModelRegistry()
    _register(
        registry,
        dependency=DependencySpec(modules=("definitely_not_installed",), level="L1"),
    )

    info = registry.find("fake.llm")
    assert info is not None
    assert info.ready is False
    assert info.missing_dependencies == ("definitely_not_installed",)

    with pytest.raises(LiteInferError) as excinfo:
        registry.ensure_ready("fake.llm")
    assert excinfo.value.code is ErrorCode.DEPENDENCY_MISSING
    assert "definitely_not_installed" in excinfo.value.details["missing"]


def test_unregistered_adapter_reports_not_found():
    registry = ModelRegistry()
    with pytest.raises(LiteInferError) as excinfo:
        registry.ensure_ready("nope")
    assert excinfo.value.code is ErrorCode.NOT_FOUND


def test_device_capability_shortage_blocks_start():
    registry = ModelRegistry(device_capability=(8, 6))  # A5000：sm86
    _register(
        registry,
        capability=describe_capabilities(
            kinds=("llm.generate",), requires_device_capability=[8, 9]
        ),
    )
    with pytest.raises(LiteInferError) as excinfo:
        registry.ensure_ready("fake.llm")
    assert excinfo.value.code is ErrorCode.UNSUPPORTED
    assert "8.9" in excinfo.value.details["detail"]


def test_unknown_device_capability_is_not_compatible():
    """设备能力未知时不能放行——未知 ≠ 兼容。"""
    registry = ModelRegistry(device_capability=None)
    _register(
        registry,
        capability=describe_capabilities(
            kinds=("llm.generate",), requires_device_capability=[8, 9]
        ),
    )
    assert registry.find("fake.llm").ready is False


def test_describe_exposes_readiness_for_cli_and_http():
    registry = ModelRegistry(device_capability=(8, 6))
    _register(registry)
    registry.register(
        WeightVerifyAdapter(),
        capability=describe_capabilities(kinds=("weights.verify",)),
        dependency=DependencySpec(level="L0"),
    )
    described = registry.describe()
    assert len(described) == 2
    by_id = {item.adapter_id: item for item in described}
    assert by_id["fake.llm"].ready is True
    assert by_id["builtin.weights-verify"].ready is True
    assert by_id["fake.llm"].to_dict()["kinds"] == ["llm.generate"]


def test_builtin_verify_adapter_is_ready_without_dependencies():
    registry = ModelRegistry()
    registry.register(
        WeightVerifyAdapter(),
        capability=describe_capabilities(kinds=("weights.verify",)),
        dependency=DependencySpec(level="L0"),
    )
    info = registry.find("builtin.weights-verify")
    assert info is not None
    assert info.ready is True
    assert info.kinds == ("weights.verify",)


def test_check_compatibility_collects_all_problems():
    report = check_compatibility(
        "x",
        api_range=">=1.0.0,<2.0.0",
        capability=describe_capabilities(requires_device_capability=[9, 0]),
        dependency=DependencySpec(modules=("nope",)),
        device_capability=(8, 6),
    )
    kinds = {item.kind for item in report.problems}
    assert kinds == {"dependency_missing", "device_capability"}


def test_unregister_clears_kind_index():
    registry = ModelRegistry()
    _register(registry)
    assert registry.unregister("fake.llm") is True
    assert registry.kinds() == []
    assert registry.unregister("fake.llm") is False


def test_conflict_helper_is_used_for_duplicates():
    """反守护：确保上面用的是领域错误而不是裸异常。"""
    with pytest.raises(LiteInferError):
        raise conflict("重复", adapter_id="x")
    with pytest.raises(LiteInferError):
        raise unsupported("不兼容", adapter_id="y")
