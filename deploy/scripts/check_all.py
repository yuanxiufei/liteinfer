#!/usr/bin/env python3
"""本地「基础检查」入口：与 CI 同一套命令，一次跑完。

对应 `.github/workflows/ci.yml` 的 lint-and-test 作业；改一边必须同步另一边，
否则本地绿、CI 红（或反过来）。CI 逐步保留是为了让每一步单独失败可定位。

用法：
    python deploy/scripts/check_all.py            # 全部检查
    python deploy/scripts/check_all.py --unit-only # 只跑单元测试（最快的内循环）
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

STEPS: list[tuple[str, list[str]]] = [
    ("ruff", [sys.executable, "-m", "ruff", "check", "."]),
    ("mypy", [sys.executable, "-m", "mypy"]),
    ("pytest -m unit", [sys.executable, "-m", "pytest", "-m", "unit", "-q"]),
    ("pytest -m integration", [sys.executable, "-m", "pytest", "-m", "integration", "-q"]),
]


def _run(name: str, command: list[str]) -> tuple[bool, str]:
    result = subprocess.run(
        command,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    tail = (result.stdout or result.stderr).strip().splitlines()
    return result.returncode == 0, tail[-1] if tail else "(无输出)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跑与 CI 一致的基础检查")
    parser.add_argument("--unit-only", action="store_true", help="只跑 ruff / mypy / 单元测试")
    args = parser.parse_args(argv)

    steps = [item for item in STEPS if not args.unit_only or item[0] != "pytest -m integration"]
    failed: list[str] = []
    for name, command in steps:
        ok, summary = _run(name, command)
        print(f"  [{'OK' if ok else 'FAIL'}] {name:<22} {summary}")
        if not ok:
            failed.append(name)

    if failed:
        print(f"\n失败：{', '.join(failed)}")
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
