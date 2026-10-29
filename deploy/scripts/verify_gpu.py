"""WBS-0.1：GPU 设备快照。

优先用 torch 读取「框架视角」显存，torch 不可用或 CUDA 不可用时退回 nvidia-smi；
两者都拿不到时明确报错，不允许用估计值冒充实测。

用法：
    python deploy/scripts/verify_gpu.py            # 人读摘要
    python deploy/scripts/verify_gpu.py --json     # JSON（stdout）
    python deploy/scripts/verify_gpu.py --source smi
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys

SMI_QUERY = "index,name,driver_version,compute_cap,memory.total,memory.used,memory.free"


def _to_int(value: str) -> int | None:
    try:
        return int(float(value))
    except ValueError:
        return None


def from_smi() -> list[dict]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        raise RuntimeError("PATH 中找不到 nvidia-smi")

    proc = subprocess.run(
        [exe, f"--query-gpu={SMI_QUERY}", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        check=True,
    )
    gpus: list[dict] = []
    for line in proc.stdout.strip().splitlines():
        parts = [item.strip() for item in line.split(",")]
        if len(parts) != len(SMI_QUERY.split(",")):
            continue
        index, name, driver, compute_cap, total, used, free = parts
        gpus.append(
            {
                "index": _to_int(index),
                "name": name,
                "driver_version": driver,
                "compute_capability": compute_cap,
                "memory_total_mib": _to_int(total),
                "memory_used_mib": _to_int(used),
                "memory_free_mib": _to_int(free),
                "source": "nvidia-smi",
            }
        )
    if not gpus:
        raise RuntimeError("nvidia-smi 未返回任何 GPU")
    return gpus


def from_torch() -> list[dict]:
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError("未安装 torch") from exc

    if not torch.cuda.is_available():
        raise RuntimeError("torch 已安装但 CUDA 不可用")

    gpus: list[dict] = []
    for index in range(torch.cuda.device_count()):
        props = torch.cuda.get_device_properties(index)
        free_bytes, total_bytes = torch.cuda.mem_get_info(torch.device(f"cuda:{index}"))
        gpus.append(
            {
                "index": index,
                "name": props.name,
                "compute_capability": f"{props.major}.{props.minor}",
                "total_memory_bytes": total_bytes,
                "free_memory_bytes": free_bytes,
                "allocated_bytes": torch.cuda.memory_allocated(index),
                "reserved_bytes": torch.cuda.memory_reserved(index),
                "torch_version": getattr(torch, "__version__", None),
                "cuda_runtime_version": getattr(torch.version, "cuda", None),
                "source": "torch",
            }
        )
    return gpus


def collect(source: str) -> tuple[list[dict], list[str]]:
    order = {"torch": [from_torch], "smi": [from_smi], "auto": [from_torch, from_smi]}[source]
    errors: list[str] = []
    for getter in order:
        try:
            return getter(), errors
        except Exception as exc:  # noqa: BLE001 - 体检脚本需要汇总所有失败原因
            errors.append(f"{getter.__name__}: {exc}")
    return [], errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LiteInfer GPU 快照（WBS-0.1）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    parser.add_argument("--source", choices=("auto", "torch", "smi"), default="auto")
    args = parser.parse_args(argv)

    gpus, errors = collect(args.source)
    payload = {
        "host": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "python_version": sys.version.split()[0],
        "gpu_count": len(gpus),
        "gpus": gpus,
        "errors": errors,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        host = payload["host"]
        print(f"Python {payload['python_version']} / {host['system']} {host['release']}")
        print(f"GPU 数量：{payload['gpu_count']}")
        for gpu in gpus:
            print(
                f"  [{gpu['index']}] {gpu['name']}  CC={gpu['compute_capability']}  "
                f"driver={gpu.get('driver_version', '-')}  source={gpu['source']}"
            )
            if "memory_total_mib" in gpu:
                print(f"       显存 total/used/free MiB = "
                      f"{gpu['memory_total_mib']}/{gpu['memory_used_mib']}/{gpu['memory_free_mib']}")
            if "total_memory_bytes" in gpu:
                total_b = gpu["total_memory_bytes"]
                free_b = gpu["free_memory_bytes"]
                print(f"       显存 total/free bytes = {total_b}/{free_b}")
        for err in errors:
            print(f"  ! {err}")

    return 0 if gpus else 1


if __name__ == "__main__":
    raise SystemExit(main())
