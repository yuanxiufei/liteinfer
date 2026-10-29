"""WBS-0.2 依赖体检：检查已安装组件与版本，供填写 env/compatibility-matrix.md。

默认只报告，不判失败；`--strict` 下缺少必需项或版本不在要求范围内才退出 1。

用法：
    python deploy/scripts/verify_dependencies.py
    python deploy/scripts/verify_dependencies.py --json --strict
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import shutil
import subprocess
import sys
from typing import Any

MIN_PYTHON = (3, 11)

# None 表示「只要存在即可」，元组表示可接受的主版本下限。
OPTIONAL_PACKAGES: dict[str, Any] = {
    "torch": None,
    "sglang": None,
    "triton": None,
    "transformers": None,
    "diffusers": None,
    "pynvml": None,
    "pytest": None,
    "ruff": None,
    "mypy": None,
}
REQUIRED_PACKAGES = ("torch", "sglang")
TOOLS = ("nvidia-smi", "uv", "docker")


def _version(module: str) -> tuple[str, bool, str]:
    spec = importlib.util.find_spec(module)
    if spec is None:
        return "not-installed", False, f"模块 {module} 未安装"
    try:
        mod = __import__(module)
    except Exception as exc:  # noqa: BLE001 - 体检需要收集导入失败原因
        return "import-failed", False, f"导入 {module} 失败：{exc}"
    return str(getattr(mod, "__version__", "unknown")), True, ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LiteInfer 依赖体检（WBS-0.2）")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true", help="缺失必需组件时退出 1")
    args = parser.parse_args(argv)

    python_ok = sys.version_info >= MIN_PYTHON
    packages = {
        name: dict(zip(("version", "ok", "note"), _version(name), strict=False))
        for name in OPTIONAL_PACKAGES
    }
    tools = {name: shutil.which(name) or "not-found" for name in TOOLS}

    if tools.get("nvidia-smi", "not-found") != "not-found":
        proc = subprocess.run(
            [tools["nvidia-smi"], "--query-gpu=name,driver_version", "--format=csv,noheader"],
            capture_output=True,
            text=True,
        )
        gpu_summary = proc.stdout.strip() or (proc.stderr.strip() or "nvidia-smi 调用失败")
    else:
        gpu_summary = "no-nvidia-smi"

    missing_required = [name for name in REQUIRED_PACKAGES if not packages[name]["ok"]]
    problems: list[str] = []
    if not python_ok:
        problems.append(
            f"Python {sys.version.split()[0]} 低于要求 {'.'.join(map(str, MIN_PYTHON))}"
        )
    problems.extend(f"缺少必需组件 {name}" for name in missing_required)

    payload = {
        "host": {"system": platform.system(), "release": platform.release()},
        "python": {"version": sys.version.split()[0], "ok": python_ok},
        "packages": packages,
        "tools": tools,
        "gpu_summary": gpu_summary,
        "problems": problems,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"Python {payload['python']['version']}（要求 >= {'.'.join(map(str, MIN_PYTHON))}）"
              f"{'  正常' if python_ok else '  不达标'}")
        for name, info in packages.items():
            print(f"  {name:<14} {info['version']}{'' if info['ok'] else '  <- ' + info['note']}")
        for name, path in tools.items():
            print(f"  {name:<14} {path}")
        print(f"  gpu            {gpu_summary}")
        for problem in problems:
            print(f"  ! {problem}")

    return 1 if args.strict and problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
