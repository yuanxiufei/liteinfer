"""适配器兼容性检查（WBS-2.3）。

纯检查，不抛异常、不做 IO：返回可读的 `CompatibilityReport`，
由调用方决定是拒绝注册、拒绝启动，还是只告警。

检查项：
1. 框架 API 版本范围（适配器声明的范围必须覆盖当前框架版本）
2. 可选依赖是否齐全（ADR-0003 的分级：缺失只关闭该能力）
3. 设备能力（例如 FP8 需要 sm89+，本机 A5000 是 sm86 ⇒ 必须报出不兼容）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .protocol import FRAMEWORK_API_VERSION, CapabilitySpec, DependencySpec, satisfies


@dataclass(frozen=True)
class CompatibilityProblem:
    """一个问题。`kind` 用于分类统计，`detail` 给人读。"""

    kind: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail}


@dataclass
class CompatibilityReport:
    """检查结果。`ok` 为真才允许加载/执行。"""

    adapter_id: str
    problems: list[CompatibilityProblem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def add(self, kind: str, detail: str) -> None:
        self.problems.append(CompatibilityProblem(kind=kind, detail=detail))

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "ok": self.ok,
            "problems": [item.to_dict() for item in self.problems],
        }


def check_framework_api(adapter_id: str, api_range: str, *, report: CompatibilityReport) -> None:
    try:
        compatible = satisfies(FRAMEWORK_API_VERSION, api_range)
    except ValueError as exc:
        report.add("framework_api", f"版本范围无法解析：{exc}")
        return
    if not compatible:
        report.add(
            "framework_api",
            f"框架 API {FRAMEWORK_API_VERSION} 不在适配器声明的范围 {api_range or '(空)'} 内",
        )


def check_dependencies(dependency: DependencySpec, *, report: CompatibilityReport) -> list[str]:
    missing = dependency.missing()
    if missing:
        report.add(
            "dependency_missing",
            f"缺少可选依赖 {missing}（级别 {dependency.level}）；"
            f"该能力关闭，核心功能不受影响",
        )
    return missing


def check_device_capability(
    capability: CapabilitySpec,
    *,
    device_capability: tuple[int, int] | None,
    report: CompatibilityReport,
) -> None:
    required = capability.requires_device_capability
    if required is None:
        return
    if device_capability is None:
        # 设备能力未知时不能放行：未知 ≠ 兼容
        report.add(
            "device_capability",
            f"适配器要求计算能力 >= {required[0]}.{required[1]}，但当前设备能力未知",
        )
        return
    if device_capability < required:
        report.add(
            "device_capability",
            f"适配器要求计算能力 >= {required[0]}.{required[1]}，"
            f"当前设备为 {device_capability[0]}.{device_capability[1]}",
        )


def check_compatibility(
    adapter_id: str,
    *,
    api_range: str,
    capability: CapabilitySpec | None = None,
    dependency: DependencySpec | None = None,
    device_capability: tuple[int, int] | None = None,
) -> CompatibilityReport:
    """一次性跑完三类检查，返回报告。"""
    report = CompatibilityReport(adapter_id=adapter_id)
    check_framework_api(adapter_id, api_range, report=report)
    if dependency is not None:
        check_dependencies(dependency, report=report)
    if capability is not None:
        check_device_capability(capability, device_capability=device_capability, report=report)
    return report
