"""版本化模型适配器与注册中心（WBS-2）。

推理适配器与训练适配器分别遵循 `ModelAdapter` / `TrainingAdapter` 协议；
注册中心负责发现、版本匹配与能力查询。

本层属于 ADR-0003 里的**适配器层**：可以依赖第三方库，
但 `core` / `runtime` / `api` / `cli` 不得反向依赖本层
（runtime 只认 `runtime.ports.AdapterLookup` 端口）。
"""

from __future__ import annotations

from .builtin import VERIFY_KIND, WeightVerifyAdapter
from .compat import CompatibilityProblem, CompatibilityReport, check_compatibility
from .protocol import (
    FRAMEWORK_API_VERSION,
    AdapterContext,
    CapabilitySpec,
    DependencySpec,
    ModelAdapter,
    TrainingAdapter,
    describe_capabilities,
    parse_version,
    satisfies,
)
from .registry import ROLE_INFERENCE, ROLE_TRAINING, AdapterEntry, ModelRegistry

__all__ = [
    "FRAMEWORK_API_VERSION",
    "ROLE_INFERENCE",
    "ROLE_TRAINING",
    "VERIFY_KIND",
    "AdapterContext",
    "AdapterEntry",
    "CapabilitySpec",
    "CompatibilityProblem",
    "CompatibilityReport",
    "DependencySpec",
    "ModelAdapter",
    "ModelRegistry",
    "TrainingAdapter",
    "WeightVerifyAdapter",
    "check_compatibility",
    "describe_capabilities",
    "parse_version",
    "satisfies",
]
