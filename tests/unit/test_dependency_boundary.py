"""依赖边界守护（ADR-0003）。

两条硬规则做成机器判据，不靠人记得：

1. `core` / `runtime` / `observability` / `api` / `cli` / `client.py`
   **不得 import 任何第三方包或具体后端**（torch / sglang / transformers / Web 框架 ……），
   只允许标准库与本包内部模块。
2. 源码中出现的 `http(s)://` 只能指向本机（回环地址或本地模板），
   即运行期不得发起外部服务调用。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "liteinfer"
GUARDED_DIRS = ("core", "runtime", "observability", "api", "cli")
GUARDED_SINGLES = ("client.py",)

# 标准库 + 本包；其余一律视为越界
ALLOWED_ROOTS = set(sys.stdlib_module_names) | {"liteinfer", "__future__"}

URL_RE = re.compile(r"https?://[^\s\"']+")
LOCAL_PREFIXES = ("127.0.0.1", "localhost", "{")  # 第三种是 f-string 拼的本机地址


def _python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _imported_roots(path: Path) -> set[str]:
    """收集文件里所有被 import 的顶层模块名（相对导入不计）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _guarded_files() -> list[Path]:
    files: list[Path] = []
    for directory in GUARDED_DIRS:
        files.extend(_python_files(SRC_ROOT / directory))
    files.extend(SRC_ROOT / name for name in GUARDED_SINGLES)
    return [path for path in files if path.exists()]


def test_guarded_layers_import_only_stdlib():
    offenders: dict[str, list[str]] = {}
    for path in _guarded_files():
        external = sorted(_imported_roots(path) - ALLOWED_ROOTS)
        if external:
            offenders[str(path.relative_to(SRC_ROOT))] = external
    assert not offenders, f"核心层不得依赖第三方或具体后端：{offenders}"


def test_no_external_urls_in_source():
    offenders: dict[str, list[str]] = {}
    for path in _python_files(SRC_ROOT):
        hits = [
            match
            for match in URL_RE.findall(path.read_text(encoding="utf-8"))
            if not match.split("://", 1)[1].startswith(LOCAL_PREFIXES)
        ]
        if hits:
            offenders[str(path.relative_to(SRC_ROOT))] = hits
    assert not offenders, f"源码中不得出现外网地址（本地调用请用回环地址）：{offenders}"


def test_guard_covers_the_layers_it_claims():
    """反守护：目录名写错会让上面两条静默变空，这里兜住。"""
    assert len(_guarded_files()) >= 10
    assert (SRC_ROOT / "core").is_dir()
