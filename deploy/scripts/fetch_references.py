#!/usr/bin/env python3
"""拉取/更新 `reference/` 下的参考项目（只供阅读，不作为依赖）。

纪律（与 WBS-0.2「先锁版本再装依赖」一致）：
1. 参考项目**不是依赖**：不写进 `pyproject.toml`，不得 `import`，
   不得复制代码到 `src/`（确需参考实现时另开 ADR 记录）。
2. 每次拉取都把 commit 记进 `reference/SOURCES.lock.json`，保证可复现。
3. 默认只补缺失的仓库，不改已锁定的 commit；要跟进上游必须显式 `--update`。
4. 只用标准库 + 本机 git，不引入新依赖。

用法：
    python deploy/scripts/fetch_references.py            # 检查现状（默认，不动盘）
    python deploy/scripts/fetch_references.py --apply    # 补齐缺失的仓库
    python deploy/scripts/fetch_references.py --update   # 跟进上游最新 commit（会改 lock）
    python deploy/scripts/fetch_references.py --all      # 含 optional 仓库（默认跳过）
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE_DIR = REPO_ROOT / "reference"
LOCK_FILE = REFERENCE_DIR / "SOURCES.lock.json"

SOURCES: list[dict[str, Any]] = [
    {
        "name": "sglang",
        "url": "https://github.com/sgl-project/sglang",
        "branch": "main",
        "wbs": "WBS-0.2 / WBS-2",
        "purpose": "推理后端真源：调度、KV 缓存、RadixAttention、取消/抢占语义",
        "optional": False,
    },
    {
        "name": "vllm",
        "url": "https://github.com/vllm-project/vllm",
        "branch": "main",
        "wbs": "WBS-3",
        "purpose": "对照实现：分页 KV、连续批处理、背压与抢占策略（只读参考，不采用为依赖）",
        "optional": False,
    },
    {
        "name": "peft",
        "url": "https://github.com/huggingface/peft",
        "branch": "main",
        "wbs": "WBS-5",
        "purpose": "LoRA 训练参考：adapter 注入、权重合并与保存格式",
        "optional": False,
    },
    {
        "name": "diffusers",
        "url": "https://github.com/huggingface/diffusers",
        "branch": "main",
        "wbs": "WBS-6",
        "purpose": "扩散训练参考：噪声/时间步采样、训练循环与显存优化",
        "optional": False,
    },
    {
        "name": "triton",
        "url": "https://github.com/triton-lang/triton",
        "branch": "main",
        "wbs": "WBS-7",
        "purpose": "算子管理参考：内核编译缓存与目标架构（本机 sm86）",
        "optional": True,
    },
]


def _git(args: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{result.stderr.strip()}")
    return result.stdout.strip()


def _head(path: Path) -> tuple[str, str]:
    commit = _git(["rev-parse", "HEAD"], cwd=path)
    date = _git(["log", "-1", "--format=%cI"], cwd=path)
    return commit, date


def _dir_size_gib(path: Path) -> float:
    total = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    return round(total / (1024**3), 2)


def _clone(source: dict[str, Any]) -> tuple[str, str]:
    target = REFERENCE_DIR / source["name"]
    _git(
        [
            "clone",
            "--depth",
            "1",
            "--branch",
            source["branch"],
            source["url"],
            str(target),
        ]
    )
    return _head(target)


def _update(source: dict[str, Any]) -> tuple[str, str]:
    target = REFERENCE_DIR / source["name"]
    _git(["fetch", "--depth", "1", "origin", source["branch"]], cwd=target)
    _git(["checkout", "--detach", "FETCH_HEAD"], cwd=target)
    return _head(target)


def _load_lock() -> dict[str, Any]:
    if not LOCK_FILE.exists():
        return {}
    return json.loads(LOCK_FILE.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="拉取参考项目到 reference/")
    parser.add_argument("--apply", action="store_true", help="补齐缺失的仓库")
    parser.add_argument("--update", action="store_true", help="跟进上游最新 commit")
    parser.add_argument("--all", action="store_true", help="包含 optional 仓库")
    args = parser.parse_args(argv)

    lock = _load_lock()
    locked = {item["name"]: item for item in lock.get("sources", [])}
    selected = [item for item in SOURCES if args.all or not item["optional"]]
    entries: list[dict[str, Any]] = []
    changed = False

    REFERENCE_DIR.mkdir(exist_ok=True)

    for source in selected:
        path = REFERENCE_DIR / source["name"]
        recorded = locked.get(source["name"])
        if not path.exists():
            if not args.apply and not args.update:
                entries.append({**source, "commit": None, "status": "missing"})
                continue
            print(f"[clone] {source['name']} ← {source['url']}")
            commit, date = _clone(source)
            status = "cloned"
            changed = True
        else:
            commit, date = _head(path)
            if args.update:
                commit, date = _update(source)
                status = "updated"
                changed = True
            else:
                status = (
                    "pinned"
                    if recorded and recorded.get("commit") == commit
                    else "present(lock 未记录或有漂移)"
                )
        entries.append(
            {
                **source,
                "path": f"reference/{source['name']}",
                "commit": commit,
                "commit_date": date,
                "size_gib": _dir_size_gib(path) if path.exists() else None,
                "status": status,
            }
        )
        print(f"  {source['name']:<10} {status:<24} {commit[:12] if commit else '-'}")

    skipped = [item["name"] for item in SOURCES if item not in selected]
    if skipped:
        print(f"  跳过 optional：{', '.join(skipped)}（加 --all 拉取）")

    if changed or not LOCK_FILE.exists():
        payload = {
            "$schema": "liteinfer/reference-sources/v1",
            "updated_at": datetime.now(UTC).strftime("%Y-%m-%d"),
            "note": "参考项目，不是依赖；不得 import，不得写入 pyproject.toml",
            "sources": [
                {key: value for key, value in entry.items() if key not in {"optional"}}
                for entry in entries
            ],
        }
        LOCK_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"已写入 {LOCK_FILE.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
