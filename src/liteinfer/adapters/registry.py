"""适配器注册中心（WBS-2.2 / 2.3）。

注册策略（与 ADR-0003 的能力分级一致）：

- **框架 API 不兼容 ⇒ 拒绝注册**（接口对不上，注册了也调不通）。
- **依赖缺失 / 设备能力不足 ⇒ 允许注册但标记 `ready=False`**，
  这样 `/v1/models` 能如实显示"已注册、缺依赖"，而不是假装不存在。
- 依赖缺失时真正**启动**任务才报错，由 `ensure_ready()` 抛出可读写法。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..core import conflict, dependency_missing, invalid_argument, not_found, unsupported
from ..runtime.ports import AdapterInfo
from .compat import check_compatibility
from .protocol import (
    FRAMEWORK_API_VERSION,
    CapabilitySpec,
    DependencySpec,
    ModelAdapter,
    TrainingAdapter,
    parse_version,
)

ROLE_INFERENCE = "inference"
ROLE_TRAINING = "training"


@dataclass(frozen=True)
class AdapterEntry:
    """一条注册记录。"""

    adapter: Any
    role: str
    capability: CapabilitySpec
    dependency: DependencySpec
    registered_at: float

    @property
    def adapter_id(self) -> str:
        return str(self.adapter.adapter_id)

    @property
    def api_version(self) -> str:
        return str(self.adapter.api_version)


class ModelRegistry:
    """注册、发现、能力查询。

    `device_capability` 形如 `(8, 6)`；未知时传 `None`，
    此时要求特定计算能力的适配器一律判为不就绪（未知 ≠ 兼容）。
    """

    def __init__(self, *, device_capability: tuple[int, int] | None = None) -> None:
        self._entries: dict[str, AdapterEntry] = {}
        self._by_kind: dict[str, list[str]] = {}
        self.device_capability = device_capability

    # ---- 注册 ----------------------------------------------------------
    def register(
        self,
        adapter: Any,
        *,
        role: str = ROLE_INFERENCE,
        capability: CapabilitySpec | None = None,
        dependency: DependencySpec | None = None,
    ) -> AdapterEntry:
        adapter_id = str(getattr(adapter, "adapter_id", "") or "").strip()
        if not adapter_id:
            raise invalid_argument("适配器必须声明非空 adapter_id")
        if adapter_id in self._entries:
            raise conflict("适配器 id 重复", adapter_id=adapter_id)

        self._check_protocol(adapter, role)

        capability = capability or CapabilitySpec()
        dependency = dependency or DependencySpec()

        # 框架 API 不兼容直接拒绝注册；依赖/设备问题只标记，不拒绝
        report = check_compatibility(
            adapter_id,
            api_range=adapter.framework_api_range,
            capability=None,
            dependency=None,
        )
        framework_problems = [
            item for item in report.problems if item.kind == "framework_api"
        ]
        if framework_problems:
            raise unsupported(
                f"适配器 {adapter_id} 与框架 API {FRAMEWORK_API_VERSION} 不兼容："
                f"{framework_problems[0].detail}",
                adapter_id=adapter_id,
            )

        entry = AdapterEntry(
            adapter=adapter,
            role=role,
            capability=capability,
            dependency=dependency,
            registered_at=time.time(),
        )
        self._entries[adapter_id] = entry
        for kind in capability.kinds:
            self._by_kind.setdefault(kind, []).append(adapter_id)
        return entry

    def _check_protocol(self, adapter: Any, role: str) -> None:
        """注册前先验证对象确实满足协议，避免后面调用到一半才发现缺方法。"""
        expected = TrainingAdapter if role == ROLE_TRAINING else ModelAdapter
        if not isinstance(adapter, expected):
            raise invalid_argument(
                f"对象不符合 {expected.__name__} 协议",
                role=role,
                missing="请实现 validate_config / load / infer / release 等方法",
            )
        try:
            parse_version(str(adapter.api_version))
        except ValueError as exc:
            raise invalid_argument(f"适配器 api_version 非法：{exc}") from exc

    def unregister(self, adapter_id: str) -> bool:
        entry = self._entries.pop(adapter_id, None)
        if entry is None:
            return False
        for kind, ids in list(self._by_kind.items()):
            self._by_kind[kind] = [item for item in ids if item != adapter_id]
            if not self._by_kind[kind]:
                del self._by_kind[kind]
        return True

    # ---- 发现 ----------------------------------------------------------
    def get(self, adapter_id: str) -> AdapterEntry | None:
        return self._entries.get(adapter_id)

    def entry_for_kind(self, kind: str) -> AdapterEntry | None:
        """按 kind 取注册记录（同一 kind 多个适配器时取最先注册的）。"""
        ids = self._by_kind.get(kind) or []
        return self._entries.get(ids[0]) if ids else None

    def kinds(self) -> list[str]:
        return sorted(self._by_kind)

    def entries(self) -> list[AdapterEntry]:
        return [self._entries[key] for key in sorted(self._entries)]

    # ---- 就绪判定 ------------------------------------------------------
    def report(self, adapter_id: str) -> Any:
        """返回该适配器的兼容性报告（不抛异常）。"""
        entry = self._entries.get(adapter_id)
        if entry is None:
            raise not_found("适配器未注册", adapter_id=adapter_id)
        return check_compatibility(
            adapter_id,
            api_range=entry.adapter.framework_api_range,
            capability=entry.capability,
            dependency=entry.dependency,
            device_capability=self.device_capability,
        )

    def ensure_ready(self, adapter_id: str) -> AdapterEntry:
        """启动前门禁：不可用就抛可读错误，绝不带着缺失依赖往下跑。"""
        entry = self._entries.get(adapter_id)
        if entry is None:
            raise not_found("适配器未注册", adapter_id=adapter_id, registered=sorted(self._entries))
        report = self.report(adapter_id)
        missing = [item for item in report.problems if item.kind == "dependency_missing"]
        if missing:
            raise dependency_missing(
                f"适配器 {adapter_id} 缺少可选依赖，无法启动",
                adapter_id=adapter_id,
                missing=entry.dependency.missing(),
                level=entry.dependency.level,
                hint="按 ADR-0003 安装对应 extras 后重试",
            )
        device = [item for item in report.problems if item.kind == "device_capability"]
        if device:
            raise unsupported(
                f"适配器 {adapter_id} 与当前设备不兼容",
                adapter_id=adapter_id,
                detail=device[0].detail,
            )
        if not report.ok:
            raise unsupported(
                f"适配器 {adapter_id} 未通过兼容性检查",
                adapter_id=adapter_id,
                problems=[item.to_dict() for item in report.problems],
            )
        return entry

    # ---- 对外快照（给 HTTP / CLI） --------------------------------------
    def info(self, entry: AdapterEntry) -> AdapterInfo:
        report = check_compatibility(
            entry.adapter_id,
            api_range=entry.adapter.framework_api_range,
            capability=entry.capability,
            dependency=entry.dependency,
            device_capability=self.device_capability,
        )
        return AdapterInfo(
            adapter_id=entry.adapter_id,
            api_version=entry.api_version,
            kinds=tuple(entry.capability.kinds),
            role=entry.role,
            ready=report.ok,
            missing_dependencies=tuple(entry.dependency.missing()),
            problems=tuple(item.detail for item in report.problems),
        )

    def find(self, adapter_id: str) -> AdapterInfo | None:
        entry = self._entries.get(adapter_id)
        return self.info(entry) if entry else None

    def find_for_kind(self, kind: str) -> AdapterInfo | None:
        """`AdapterLookup` 端口要求的方法：按 kind 取快照。"""
        entry = self.entry_for_kind(kind)
        return self.info(entry) if entry else None

    def describe(self) -> list[AdapterInfo]:
        return [self.info(entry) for entry in self.entries()]
