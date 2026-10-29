#!/usr/bin/env bash
# WBS-0.2：在 WSL2 Ubuntu-24.04 内按 env/candidate-lock.json 实装并验证候选版本组合。
#
# 纪律（与基线「先锁版本再装依赖」一致）：
#   1. 本脚本只做「实装 + 验证 + 留证据」，**不修改 pyproject.toml，也不生成 uv.lock**。
#      那一步必须在实测通过后由人决定，避免把未验证的组合写进锁定文件。
#   2. 候选 pin 来自 reference/ 里的上游源码，commit 记在 env/candidate-lock.json；
#      实测结论写回 env/runtime-versions.json 与 env/compatibility-matrix.md（人工）。
#   3. 失败就停：不降级、不换版本、不"先装最新版再修冲突"。
#
# 用法：
#   bash deploy/scripts/lock_dependencies.sh            # 实装并验证
#   bash deploy/scripts/lock_dependencies.sh --dry-run  # 只打印将执行的命令
#   VENV=~/venvs/liteinfer bash deploy/scripts/lock_dependencies.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CANDIDATE_LOCK="$REPO_ROOT/env/candidate-lock.json"
VENV="${VENV:-$HOME/venvs/liteinfer}"
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

log() { printf '%s\n' "$*"; }
die() { printf '失败：%s\n' "$*" >&2; exit 1; }

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

# ---- 前置检查 ----------------------------------------------------------------
command -v python3 >/dev/null || die "需要 python3"
command -v uv >/dev/null || die "需要 uv（安装：curl -LsSf https://astral.sh/uv/install.sh | sh）"
[ -f "$CANDIDATE_LOCK" ] || die "缺少 $CANDIDATE_LOCK"

kernel="$(uname -r)"
case "$kernel" in
  *icrosoft*|*WSL*) log "内核：$kernel（WSL2）" ;;
  *) log "警告：内核 $kernel 不像 WSL2；正式 GPU 验收环境为 WSL2（ADR-0001）" ;;
esac

if command -v nvidia-smi >/dev/null; then
  log "GPU：$(nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader)"
else
  log "警告：未找到 nvidia-smi，无法确认 GPU 透传"
fi

# ---- 读出候选 pin ------------------------------------------------------------
read_pins() {
  python3 - "$CANDIDATE_LOCK" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
if data.get("status") != "candidate-unverified":
    print(f"注意：candidate-lock.json 状态为 {data.get('status')}", file=sys.stderr)
for item in data["packages"]:
    print(f"{item['name']}{item['pin']}")
PY
}

PINS="$(read_pins)"
log "候选 pin（$(printf '%s' "$PINS" | wc -l | tr -d ' ') 项，来源 $(python3 -c "import json;d=json.load(open('$CANDIDATE_LOCK',encoding='utf-8'));print(d['source']['repo']+'@'+d['source']['commit'][:12])"))："
printf '  %s\n' $PINS

# ---- 建虚拟环境并实装 --------------------------------------------------------
if [ ! -d "$VENV" ]; then
  run uv venv "$VENV" --python 3.12
fi
PY="$VENV/bin/python"
log "虚拟环境：$VENV（$($PY -V 2>&1 || echo '待创建')）"

# shellcheck disable=SC2086
run uv pip install --python "$PY" $PINS
run uv pip install --python "$PY" -e "$REPO_ROOT"

# ---- 验证 --------------------------------------------------------------------
log "依赖体检："
run "$PY" - <<'PY'
import torch
print("torch", torch.__version__, "cuda_available", torch.cuda.is_available())
if torch.cuda.is_available():
    print("device", torch.cuda.get_device_name(0))
try:
    import sglang
    print("sglang", getattr(sglang, "__version__", "unknown"))
except Exception as exc:  # 体检要收集失败原因，不吞掉
    print("sglang 导入失败：", exc)
PY

run "$PY" "$REPO_ROOT/deploy/scripts/verify_dependencies.py"

log ""
log "下一步（人工，不要脚本代劳）："
log "  1. 把实测通过的版本写进 env/runtime-versions.json 的 runtime 段"
log "  2. 把同一组版本写进 pyproject.toml 的 dependencies（此前有意为空）"
log "  3. uv lock 生成 uv.lock，并记录 uv 版本与 uv.lock 哈希"
log "  4. 回填 env/compatibility-matrix.md：候选 → 已实测"
