#!/usr/bin/env bash
# LiteInfer 环境体检（Linux 容器 / WSL2）。Windows 主机请用 verify_gpu.py / verify_dependencies.py。
# 检查项失败只做标记，不中途退出，最后汇总。
set -uo pipefail

FAILED=0
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

pass() { printf '  [OK]   %s\n' "$1"; }
warn() { printf '  [WARN] %s: %s\n' "$1" "$2"; FAILED=1; }

echo "LiteInfer doctor — $(date -Is 2>/dev/null || date)"
echo "项目根目录：${ROOT}"

# 1. GPU 设备
if command -v nvidia-smi >/dev/null 2>&1; then
  pass "nvidia-smi"
  nvidia-smi --query-gpu=index,name,driver_version,memory.total --format=csv,noheader 2>/dev/null | sed 's/^/         /'
else
  warn "nvidia-smi" "未找到，容器中需确认 GPU 透传"
fi

# 2. Python 解释器和 uv
if command -v python3 >/dev/null 2>&1; then
  PY_VER="$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
  pass "python3 ${PY_VER}"
else
  warn "python3" "未找到"
fi

if command -v uv >/dev/null 2>&1; then
  pass "uv $(uv --version 2>/dev/null)"
else
  warn "uv" "未安装，依赖锁定需要它"
fi

# 3. 依赖体检
if python3 "${SCRIPT_DIR}/verify_dependencies.py" >/dev/null 2>&1; then
  pass "verify_dependencies.py"
else
  warn "verify_dependencies.py" "返回非零，请查看输出"
fi

# 4. GPU 快照
if python3 "${SCRIPT_DIR}/verify_gpu.py" >/dev/null 2>&1; then
  pass "verify_gpu.py"
else
  warn "verify_gpu.py" "未拿到 GPU 快照"
fi

# 5. 磁盘空间（Swap 与冷权重需要可观容量）
if df -h "${ROOT}" >/dev/null 2>&1; then
  AVAIL="$(df -h "${ROOT}" | awk 'NR==2 {print $4}')"
  pass "工作目录可用空间 ${AVAIL}"
else
  warn "df" "无法读取磁盘信息"
fi

# 6. Swap 目标目录（启用 Swap 前必须是真实高速 NVMe）
for dir in /data/cold-weights /mnt/nvme/liteinfer-swap; do
  if [ -d "${dir}" ]; then
    printf '  [INFO] %s 已存在\n' "${dir}"
  else
    printf '  [INFO] %s 未创建（Swap 当前默认关闭）\n' "${dir}"
  fi
done

echo
if [ "${FAILED}" -eq 0 ]; then
  echo "体检完成：无阻断项。"
else
  echo "体检完成：存在告警项，请先解决再进入下一阶段。"
fi
exit 0
