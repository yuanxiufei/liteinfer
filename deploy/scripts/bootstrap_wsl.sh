#!/usr/bin/env bash
# LiteInfer · WSL2 Ubuntu-24.04 环境引导（WBS-0）
#
# 职责：
#   1. 核验 WSL2 发行版、GPU 透传、内存与磁盘是否满足正式 GPU 验收环境要求；
#   2. 创建独立的 Python 虚拟环境；
#   3. 按需安装候选依赖并输出版本快照。
#
# 纪律：不在宿主机安装任何东西；不写死依赖版本（候选由环境变量传入）；结果可复现。
#
# Windows PowerShell 调用方式：
#   wsl.exe -d Ubuntu-24.04 -- bash /mnt/d/code/voide/liteinfer/deploy/scripts/bootstrap_wsl.sh
#   wsl.exe -d Ubuntu-24.04 -- bash /mnt/d/code/voide/liteinfer/deploy/scripts/bootstrap_wsl.sh --with-deps
#   wsl.exe -d Ubuntu-24.04 -- bash /mnt/d/code/voide/liteinfer/deploy/scripts/bootstrap_wsl.sh --with-deps --background
#   wsl.exe -d Ubuntu-24.04 -- tail -20 /tmp/liteinfer-bootstrap.log

set -euo pipefail

VENV="${LITEINFER_VENV:-$HOME/venvs/liteinfer}"
PY_VERSION="${LITEINFER_PYTHON:-3.12}"
TORCH_INDEX_URL="${LITEINFER_TORCH_INDEX:-https://download.pytorch.org/whl/cu130}"
TORCH_PACKAGES="${LITEINFER_TORCH_PACKAGES:-torch torchvision}"
EXTRA_PACKAGES="${LITEINFER_EXTRA_PACKAGES:-}"
LOG_FILE="${LITEINFER_BOOTSTRAP_LOG:-/tmp/liteinfer-bootstrap.log}"

WITH_DEPS=0
BACKGROUND=0

for arg in "$@"; do
  case "$arg" in
    --with-deps) WITH_DEPS=1 ;;
    --background) BACKGROUND=1 ;;
    --help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -h) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：$arg（用 --help 查看用法）" >&2; exit 2 ;;
  esac
done

if [[ $BACKGROUND -eq 1 ]]; then
  args=()
  for arg in "$@"; do
    [[ "$arg" == "--background" ]] || args+=("$arg")
  done
  setsid nohup "$0" "${args[@]+"${args[@]}"}" >>"$LOG_FILE" 2>&1 </dev/null &
  echo "已在后台启动，日志：$LOG_FILE"
  exit 0
fi

say() { printf '\n== %s ==\n' "$1"; }

say "发行版"
grep -E '^(PRETTY_NAME|VERSION_ID)=' /etc/os-release
uname -sr

say "GPU 透传"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=index,name,driver_version,compute_cap,memory.total --format=csv,noheader
else
  echo "未找到 nvidia-smi：WSL2 GPU 透传不可用，不能作为正式 GPU 验收环境" >&2
fi
ls /usr/lib/wsl/lib/libcuda.so.1 2>/dev/null || echo "警告：/usr/lib/wsl/lib/libcuda.so.1 缺失，CUDA 用户态库不可用"

say "资源"
free -g | head -2
df -h / | tail -1
echo "说明：WSL2 的根文件系统是 ext4.vhdx 虚拟磁盘，不等于直通 NVMe；Swap 目录选它需要额外说明。"
lsblk -d -o NAME,SIZE,ROTA,MODEL 2>/dev/null || true

# uv 常装在 ~/.local/bin，非 login shell 下不在 PATH 中
export PATH="$HOME/.local/bin:$PATH"
UV_BIN="$(command -v uv || true)"
if [[ -z "$UV_BIN" ]]; then
  echo "未找到 uv：请先安装 uv（https://docs.astral.sh/uv/getting-started/installation/）" >&2
  exit 3
fi

say "虚拟环境"
if [[ -x "$VENV/bin/python" ]]; then
  echo "已存在：$VENV"
else
  "$UV_BIN" venv "$VENV" --python "$PY_VERSION"
fi
"$VENV/bin/python" -V
"$UV_BIN" --version

versions_json() {
  "$VENV/bin/python" -c '
import importlib, json, sys
names = ["torch", "torchvision", "sglang", "transformers", "diffusers", "accelerate", "numpy"]
out = {}
for n in names:
    try:
        out[n] = getattr(importlib.import_module(n), "__version__", "unknown")
    except Exception:
        out[n] = None
out["__python__"] = sys.version.split()[0]
try:
    import torch
    out["__cuda_available__"] = torch.cuda.is_available()
    out["__cuda_runtime__"] = torch.version.cuda
    out["__device__"] = torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
except Exception:
    out["__cuda_available__"] = False
print(json.dumps(out, ensure_ascii=False, indent=2))
'
}

if [[ $WITH_DEPS -eq 1 ]]; then
  say "安装候选依赖"
  echo "torch index = $TORCH_INDEX_URL"
  # shellcheck disable=SC2086
  "$UV_BIN" pip install --python "$VENV/bin/python" --index-url "$TORCH_INDEX_URL" $TORCH_PACKAGES
  if [[ -n "$EXTRA_PACKAGES" ]]; then
    # shellcheck disable=SC2086
    "$UV_BIN" pip install --python "$VENV/bin/python" $EXTRA_PACKAGES
  fi
fi

say "版本快照"
versions_json

say "完成"
echo "后续建议：把本脚本输出的快照归档到 env/wsl-snapshot.json，并在 env/compatibility-matrix.md 登记结论。"
