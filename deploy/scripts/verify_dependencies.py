"""WBS-0.2 依赖体检：检查已安装组件与版本，供填写 env/compatibility-matrix.md。

默认只报告，不判失败；`--strict` 下缺少必需项或版本不在要求范围内才退出 1。
另会读取 `env/candidate-lock.json`（候选 pin，未实测）与实装版本对照，
但候选不符**不计入 problems**——未实测的 pin 不是失败依据。

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
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
CANDIDATE_LOCK = REPO_ROOT / "env" / "candidate-lock.json"

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


def _load_candidate() -> dict[str, Any] | None:
    if not CANDIDATE_LOCK.exists():
        return None
    return json.loads(CANDIDATE_LOCK.read_text(encoding="utf-8"))


def _match_pin(installed: str, pin: str) -> str:
    """候选 pin 与实装版本对照；范围约束不自动比对，避免假结论。"""
    if installed in ("not-installed", "import-failed", "unknown"):
        return installed
    if pin.startswith("=="):
        return "一致" if installed == pin[2:] else f"不一致（实装 {installed}）"
    if pin.startswith("["):  # 形如 [cu13]==0.7.2
        exact = pin.split("==", 1)[1] if "==" in pin else None
        return "一致" if exact and installed == exact else f"需人工核对（实装 {installed}）"
    return f"范围约束 {pin}，需人工核对（实装 {installed}）"


def _candidate_report(candidate: dict[str, Any] | None) -> dict[str, Any]:
    if candidate is None:
        return {"status": "missing", "note": f"未找到 {CANDIDATE_LOCK.name}"}
    source = candidate.get("source", {})
    packages = []
    for item in candidate.get("packages", []):
        installed, _ok, _note = _version(item["name"])
        packages.append(
            {
                "name": item["name"],
                "pin": item["pin"],
                "installed": installed,
                "match": _match_pin(installed, item["pin"]),
                "purpose": item.get("purpose", ""),
            }
        )
    return {
        "status": candidate.get("status", "unknown"),
        "source": f"{source.get('repo', '?')}@{str(source.get('commit', '?'))[:12]}",
        "python_requires": candidate.get("python", {}).get("requires"),
        "packages": packages,
        "excluded": candidate.get("excluded", []),
    }


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
        "candidate": _candidate_report(_load_candidate()),
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

        candidate = payload["candidate"]
        if candidate.get("status") == "missing":
            print(f"  候选 pin       {candidate['note']}")
        else:
            print(
                f"  候选 pin（{candidate['status']}，出处 {candidate['source']}，"
                f"Python {candidate['python_requires']}）"
            )
            for item in candidate["packages"]:
                print(
                    f"    {item['name']:<18} pin {item['pin']:<18} 实装 {item['installed']:<14}"
                    f" {item['match']}"
                )
            for item in candidate.get("excluded", []):
                print(f"    （不装）{item['name']}：{item['reason']}")

        for problem in problems:
            print(f"  ! {problem}")

    return 1 if args.strict and problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
