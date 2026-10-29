"""runtime 面向外部的端口（WBS-2 接入用）。

这是为了守住 ADR-0003 的依赖方向：**`runtime` 不 import `adapters`**，
只认这里的窄协议，由组合根（`bootstrap.py`）把真正的注册中心注入进来。

`AdapterInfo` 是领域层能理解的事实快照，不含任何后端对象。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class AdapterInfo:
    """适配器的事实快照：注册了什么、能不能用、为什么不能用。"""

    adapter_id: str
    api_version: str
    kinds: tuple[str, ...] = ()
    role: str = "inference"
    ready: bool = False
    missing_dependencies: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "adapter_id": self.adapter_id,
            "api_version": self.api_version,
            "kinds": list(self.kinds),
            "role": self.role,
            "ready": self.ready,
            "missing_dependencies": list(self.missing_dependencies),
            "problems": list(self.problems),
        }


@runtime_checkable
class AdapterLookup(Protocol):
    """任务服务需要的唯一能力：按 id / kind 查适配器，并判断是否可用。"""

    def find(self, adapter_id: str) -> AdapterInfo | None: ...

    def find_for_kind(self, kind: str) -> AdapterInfo | None: ...

    def describe(self) -> list[AdapterInfo]: ...
