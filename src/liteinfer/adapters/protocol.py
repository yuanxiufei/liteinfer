"""适配器契约与版本规则（WBS-2.1 / 5.2）。

纪律：
1. 本模块与 `registry.py` 属于**适配器层**：可以依赖第三方库，
   但 `core` / `runtime` / `api` / `cli` 不得反向依赖本层（ADR-0003）。
2. 推理适配器与训练适配器是**两套独立协议**：不能要求所有推理模型都实现训练方法（基线 §5.1）。
3. 版本规则：MAJOR 不兼容、MINOR 新增可选能力、PATCH 兼容性修复；
   适配器用范围表达式声明它支持的框架 API（如 `>=1.0,<2.0`）。
"""

from __future__ import annotations

import importlib.util
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

#: 本框架当前实现的适配器 API 版本。适配器必须声明它兼容的范围。
FRAMEWORK_API_VERSION = "1.0.0"

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_CONDITION_RE = re.compile(r"^(>=|<=|==|!=|>|<)?\s*(\d+)\.(\d+)\.(\d+)$")


def parse_version(text: str) -> tuple[int, int, int]:
    """解析 `MAJOR.MINOR.PATCH`。不接受 `latest` 之类的模糊写法。"""
    match = _VERSION_RE.match(text.strip())
    if not match:
        raise ValueError(f"版本号必须是 MAJOR.MINOR.PATCH：{text!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _compare(left: tuple[int, int, int], right: tuple[int, int, int]) -> int:
    return (left > right) - (left < right)


def satisfies(version: str, expression: str) -> bool:
    """判断版本是否满足范围表达式，如 `>=1.0.0,<2.0.0`。

    空表达式视为无约束。不支持 `~=` / `^` 之类花式写法——范围必须写清楚。
    """
    expr = expression.strip()
    if not expr:
        return True
    target = parse_version(version)
    for raw in expr.split(","):
        clause = raw.strip()
        if not clause:
            continue
        match = _CONDITION_RE.match(clause)
        if not match:
            raise ValueError(f"无法解析版本约束：{clause!r}")
        operator = match.group(1) or "=="
        other = (int(match.group(2)), int(match.group(3)), int(match.group(4)))
        order = _compare(target, other)
        ok = {
            ">=": order >= 0,
            "<=": order <= 0,
            ">": order > 0,
            "<": order < 0,
            "==": order == 0,
            "!=": order != 0,
        }[operator]
        if not ok:
            return False
    return True


@dataclass(frozen=True)
class DependencySpec:
    """适配器运行所需的可选依赖（对应 ADR-0003 的能力分级）。

    `modules` 是可导入模块名（如 `torch`、`sglang`）；缺失时适配器不可用，
    但**不影响核心**，只关闭对应能力。
    """

    modules: tuple[str, ...] = ()
    level: str = "L1"
    note: str = ""

    def missing(self) -> list[str]:
        """返回当前环境缺失的模块。只做存在性探测，不导入（导入很贵且可能有副作用）。"""
        return [name for name in self.modules if importlib.util.find_spec(name) is None]

    def to_dict(self) -> dict[str, Any]:
        return {"modules": list(self.modules), "level": self.level, "note": self.note}


@dataclass(frozen=True)
class CapabilitySpec:
    """适配器声明的能力与硬件要求。

    `requires_device_capability`：需要的最低 CUDA 计算能力（如 FP8 要 8.9）。
    缺省表示无特殊要求。
    """

    kinds: tuple[str, ...] = ()
    modalities: tuple[str, ...] = ()
    requires_device_capability: tuple[int, int] | None = None
    weight_formats: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "kinds": list(self.kinds),
            "modalities": list(self.modalities),
            "requires_device_capability": (
                list(self.requires_device_capability)
                if self.requires_device_capability
                else None
            ),
            "weight_formats": list(self.weight_formats),
        }


@dataclass
class AdapterContext:
    """交给适配器 `load()` 的上下文。适配器只能读，不得改任务状态。"""

    task_id: str | None = None
    cancel_event: Any = None
    logger: Any = None
    metrics: Any = None
    config: Mapping[str, Any] = field(default_factory=dict)

    def check_cancel(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            from ..core import TaskCancelled

            raise TaskCancelled("适配器在安全点检测到取消请求")


@runtime_checkable
class ModelAdapter(Protocol):
    """推理适配器最小协议（基线 §5.1）。"""

    adapter_id: str
    api_version: str
    #: 该适配器兼容的框架 API 范围（如 `>=1.0.0,<2.0.0`）
    framework_api_range: str

    def validate_config(self, config: Mapping[str, Any]) -> None:
        """校验配置；非法配置必须抛 `LiteInferError`，不得静默忽略。"""

    def load(self, context: AdapterContext) -> None: ...

    def infer(self, request: Any) -> Any: ...

    def release(self) -> None: ...


@runtime_checkable
class TrainingAdapter(Protocol):
    """训练适配器协议。与推理适配器**相互独立**（基线 §5.1）。"""

    adapter_id: str
    api_version: str
    framework_api_range: str

    def validate_config(self, config: Mapping[str, Any]) -> None: ...

    def load(self, context: AdapterContext) -> None: ...

    def forward(self, batch: Any) -> Any: ...

    def loss(self, outputs: Any, batch: Any) -> Any: ...

    def backward(self, loss: Any) -> None: ...

    def optimizer_step(self) -> None: ...

    def save_checkpoint(self, path: str) -> None: ...

    def load_checkpoint(self, path: str) -> None: ...

    def release(self) -> None: ...


def describe_capabilities(
    kinds: Iterable[str] = (),
    modalities: Iterable[str] = (),
    *,
    requires_device_capability: Sequence[int] | None = None,
    weight_formats: Iterable[str] = (),
) -> CapabilitySpec:
    """便捷构造：把可迭代对象固化成不可变元组。"""
    device = tuple(requires_device_capability) if requires_device_capability else None
    if device is not None and len(device) != 2:
        raise ValueError("requires_device_capability 必须是 (major, minor)")
    return CapabilitySpec(
        kinds=tuple(kinds),
        modalities=tuple(modalities),
        requires_device_capability=(int(device[0]), int(device[1])) if device else None,
        weight_formats=tuple(weight_formats),
    )
