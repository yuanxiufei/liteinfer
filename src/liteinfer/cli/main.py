"""命令行入口（WBS-1.5）。

命令：doctor / serve / model list / infer / task {status,cancel,preempt,resume,list}
纪律：CLI 与 HTTP 服务调用同一套核心领域服务；`doctor` 不得伪造 GPU 结论。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .. import __version__
from ..bootstrap import build_service
from ..core import ErrorCode, LiteInferConfig, LiteInferError, invalid_argument
from ..core.config import config_from_mapping
from ..runtime import build_meter

DEFAULT_URL = "http://127.0.0.1:8000"
REPO_ROOT = Path(__file__).resolve().parents[3]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="liteinfer", description="LiteInfer 命令行")
    parser.add_argument("--version", action="version", version=f"liteinfer {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="环境体检（不执行 GPU 验收）")

    serve = sub.add_parser("serve", help="启动独立 HTTP 服务")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--config", help="JSON 配置文件路径")

    models = sub.add_parser("model", help="模型与适配器")
    model_sub = models.add_subparsers(dest="action", required=True)
    model_list = model_sub.add_parser("list", help="列出已注册适配器")
    model_list.add_argument("--url", default=DEFAULT_URL)
    model_list.add_argument("--json", action="store_true")

    infer = sub.add_parser("infer", help="提交一次推理任务并等待结果")
    infer.add_argument("--kind", required=True, help="任务类型（骨架期仅 echo 可用）")
    infer.add_argument("--payload", default="{}", help="JSON 字符串形式的 payload")
    infer.add_argument("--url", default=DEFAULT_URL)
    infer.add_argument("--tenant", default="default")
    infer.add_argument("--client-token", default=None, help="幂等键，重试不产生重复任务")
    infer.add_argument("--timeout", type=float, default=60.0)

    task = sub.add_parser("task", help="任务查询与取消")
    task_sub = task.add_subparsers(dest="action", required=True)

    status = task_sub.add_parser("status", help="查询任务状态")
    status.add_argument("task_id")
    status.add_argument("--url", default=DEFAULT_URL)

    cancel = task_sub.add_parser("cancel", help="取消任务")
    cancel.add_argument("task_id")
    cancel.add_argument("--url", default=DEFAULT_URL)

    preempt = task_sub.add_parser("preempt", help="请求抢占（仅在声明安全点的执行器上受理）")
    preempt.add_argument("task_id")
    preempt.add_argument("--url", default=DEFAULT_URL)

    resume = task_sub.add_parser("resume", help="从安全点续跑被抢占的任务")
    resume.add_argument("task_id")
    resume.add_argument("--url", default=DEFAULT_URL)

    listing = task_sub.add_parser("list", help="列出任务")
    listing.add_argument("--url", default=DEFAULT_URL)
    listing.add_argument("--status", default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            return cmd_doctor()
        if args.command == "serve":
            return cmd_serve(args)
        if args.command == "model":
            return cmd_model_list(args)
        if args.command == "infer":
            return cmd_infer(args)
        if args.command == "task":
            return cmd_task(args)
    except LiteInferError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    parser.error("未知命令")
    return 2


# ---------------------------------------------------------------------- doctor


def cmd_doctor() -> int:
    print(f"LiteInfer doctor — 版本 {__version__}")
    print(f"项目根目录：{REPO_ROOT}")

    major, minor = sys.version_info[:2]
    ok = (major, minor) >= (3, 11)
    _line(ok, f"Python {major}.{minor}（requires-python >= 3.11）")

    try:
        LiteInferConfig().validate()
        _line(True, "默认配置校验通过")
    except LiteInferError as exc:
        _line(False, f"默认配置校验失败：{exc}")

    for relative in (
        "pyproject.toml",
        "env/hardware-report.json",
        "env/compatibility-matrix.md",
        "env/runtime-versions.json",
        "env/benchmark-baseline.json",
        "deploy/scripts/verify_gpu.py",
        "deploy/scripts/verify_dependencies.py",
        "deploy/scripts/doctor.sh",
    ):
        _line((REPO_ROOT / relative).exists(), f"存在 {relative}")

    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            output = subprocess.run(
                [
                    nvidia_smi,
                    "--query-gpu=name,driver_version,memory.total",
                    "--format=csv,noheader",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
            _line(True, f"nvidia-smi：{output.stdout.strip()}")
            print("  [INFO] Windows 主机不参与正式 GPU 验收")
            print("  [INFO] 正式环境见 env/compatibility-matrix.md")
        except (OSError, subprocess.SubprocessError) as exc:
            _line(False, f"nvidia-smi 调用失败：{exc}")
    else:
        print("  [INFO] 未找到 nvidia-smi：本进程不产出 GPU 结论")
        print("  [INFO] 请用 deploy/scripts/verify_gpu.py 在正式环境采集")

    print("  [INFO] 依赖组合尚未锁定（WBS-0.2）：运行时依赖保持为空是有意为之")

    print("资源采样（WBS-3.4：三类分开报，测不到就说测不到）")
    report = build_meter(disk_path=REPO_ROOT).report()
    for kind, label in (("vram", "显存"), ("ram", "内存"), ("ssd", "磁盘")):
        info = report[kind]
        if info["measured"]:
            _line(True, f"{label}采样：{info['bytes']} 字节（来源 {info['source']}）")
        else:
            print(f"  [INFO] {label}采样：本进程没测到（{info['source']}），不作为结论")
    return 0


def _line(ok: bool, text: str) -> None:
    print(f"  [{'OK' if ok else 'WARN'}] {text}")


# ---------------------------------------------------------------------- serve


def cmd_serve(args: argparse.Namespace) -> int:
    config = _load_config(getattr(args, "config", None))
    # 组合根负责装配适配器与执行器：CLI 不直接依赖适配器层（ADR-0003）
    service = build_service(config)
    from ..api import run_server

    print(f"启动 HTTP 服务：{args.host}:{args.port}（Ctrl+C 停止）")
    try:
        run_server(service, host=args.host, port=args.port, version=__version__)
    except KeyboardInterrupt:
        service.close()
    return 0


def _load_config(path: str | None) -> LiteInferConfig:
    if path is None:
        return LiteInferConfig()
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise invalid_argument("配置文件必须是 JSON 对象", path=path)
    return config_from_mapping(raw)


# ---------------------------------------------------------------------- 客户端命令


def _request(method: str, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(  # noqa: S310 - 地址由命令行参数给定
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"error": {"code": ErrorCode.INTERNAL.value, "message": raw}}
        error = payload.get("error", {})
        raise LiteInferError(
            ErrorCode(error.get("code", ErrorCode.INTERNAL.value)),
            error.get("message", "请求失败"),
            details={"http_status": exc.code},
        ) from exc
    except urllib.error.URLError as exc:
        raise LiteInferError(
            ErrorCode.INTERNAL, f"无法连接服务 {url}：{exc.reason}", retryable=True
        ) from exc


def cmd_model_list(args: argparse.Namespace) -> int:
    payload = _request("GET", f"{args.url}/v1/models")
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(payload.get("note", ""))
        for model in payload.get("models", []):
            print(f"  - {model}")
        if not payload.get("models"):
            print("  （无已注册适配器）")
    return 0


def cmd_infer(args: argparse.Namespace) -> int:
    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as exc:
        raise invalid_argument(f"--payload 不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise invalid_argument("--payload 必须是 JSON 对象")

    submitted = _request(
        "POST",
        f"{args.url}/v1/tasks",
        {
            "kind": args.kind,
            "payload": payload,
            "tenant": args.tenant,
            "client_token": args.client_token,
        },
    )
    task_id = submitted["task"]["task_id"]
    deadline = time.monotonic() + args.timeout
    while True:
        current = _request("GET", f"{args.url}/v1/tasks/{task_id}")["task"]
        if current["status"] in {"succeeded", "failed", "cancelled", "reclaimed"}:
            print(json.dumps(current, ensure_ascii=False, indent=2))
            return 0 if current["status"] == "succeeded" else 1
        if time.monotonic() > deadline:
            print(json.dumps(current, ensure_ascii=False, indent=2))
            print(f"等待超时（{args.timeout}s），任务仍未终结", file=sys.stderr)
            return 1
        time.sleep(0.05)


def cmd_task(args: argparse.Namespace) -> int:
    if args.action == "status":
        payload = _request("GET", f"{args.url}/v1/tasks/{args.task_id}")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.action == "cancel":
        payload = _request("POST", f"{args.url}/v1/tasks/{args.task_id}/cancel")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.action in ("preempt", "resume"):
        payload = _request("POST", f"{args.url}/v1/tasks/{args.task_id}/{args.action}")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.action == "list":
        url = f"{args.url}/v1/tasks"
        if args.status:
            url = f"{url}?status={args.status}"
        payload = _request("GET", url)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    return 2
