"""版本规则与依赖声明（WBS-2.1 / 5.2）。"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from liteinfer.adapters import (
    DependencySpec,
    describe_capabilities,
    parse_version,
    satisfies,
)

pytestmark = pytest.mark.unit


def test_parse_version_requires_three_parts():
    assert parse_version("1.2.3") == (1, 2, 3)
    with pytest.raises(ValueError):
        parse_version("1.2")
    with pytest.raises(ValueError):
        parse_version("latest")


@pytest.mark.parametrize(
    ("version", "expression", "expected"),
    [
        ("1.0.0", ">=1.0.0,<2.0.0", True),
        ("2.0.0", ">=1.0.0,<2.0.0", False),
        ("1.5.0", ">=1.0.0,<2.0.0", True),
        ("0.9.9", ">=1.0.0", False),
        ("1.0.0", "==1.0.0", True),
        ("1.0.0", "", True),  # 空表达式视为无约束
        ("1.0.0", "!=1.0.0", False),
    ],
)
def test_satisfies(version: str, expression: str, expected: bool):
    assert satisfies(version, expression) is expected


def test_satisfies_rejects_unparsable_clause():
    with pytest.raises(ValueError):
        satisfies("1.0.0", "~1.0")


def test_dependency_spec_reports_missing_modules(
    missing_modules: Callable[..., None],
) -> None:
    missing_modules("torch", "sglang")
    spec = DependencySpec(modules=("torch", "sglang"), level="L1")
    assert spec.missing() == ["torch", "sglang"]


def test_dependency_spec_empty_is_always_satisfied():
    assert DependencySpec().missing() == []


def test_describe_capabilities_freezes_iterables():
    capability = describe_capabilities(
        ["llm.generate"], modalities=["text"], requires_device_capability=[8, 9]
    )
    assert capability.kinds == ("llm.generate",)
    assert capability.requires_device_capability == (8, 9)


def test_describe_capabilities_validates_device_shape():
    with pytest.raises(ValueError):
        describe_capabilities(requires_device_capability=[8])  # type: ignore[list-item]
