from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture()
def missing_modules(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """把指定模块名当作「本机没装」，与真实环境解耦。

    依赖门禁用例的前提是「某些可选依赖不存在」——这个前提必须由用例自己保证，
    不能指望宿主机恰好没装 torch。本机一旦装了 torch，原本的门禁用例就会静默失去意义
    （实际报错正是：`assert spec.missing() == ["torch", "sglang"]` 只剩 `["sglang"]`）。
    """

    def hide(*names: str) -> None:
        hidden = set(names)
        real = importlib.util.find_spec

        def fake(name: str, *args: Any, **kwargs: Any) -> Any:
            if name in hidden:
                return None
            return real(name, *args, **kwargs)

        monkeypatch.setattr(importlib.util, "find_spec", fake)

    return hide
