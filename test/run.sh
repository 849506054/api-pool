#!/usr/bin/env bash
# API Pool 统一测试入口：固定解释器与依赖，自建隔离副本，命令只看测试文件名。
#
#   test/run.sh test_endpoint_config_persistence.py test_endpoint_save_feedback.js
#   test/run.sh 'test_credential_redaction*'
#   test/run.sh --pytest        # 协议函数集（pytest 形态）
#   test/run.sh --all           # 全量：unittest + pytest + Node + Gemini
#   test/run.sh --list          # 列出可跑文件
#
# 选项：
#   --keep        保留隔离副本并打印路径（排查用）
#   --no-install  缺依赖时直接报错，不自动安装
#   -h, --help    显示帮助
#
# 导入 agent.* 的契约测试自动换 /opt/hermes 的解释器跑，并在仓库内备好
# 对应版本的 brotli / zstandard（.test-deps-py<版本>/），不碰 Hermes 自己的环境。
#
# 解释器优先级：$PYTHON > 仓库 .venv/bin/python。只在项目 .venv 里自动补依赖，
# 不改系统或宿主 Python。

set -uo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DEFAULT_PY="$ROOT/.venv/bin/python"
PY="${PYTHON:-$DEFAULT_PY}"
HERMES_PY="${HERMES_PYTHON:-/opt/hermes/.venv/bin/python}"
HERMES_TAG=""
if [ -x "$HERMES_PY" ]; then
  HERMES_TAG=$("$HERMES_PY" -c 'import sys; print(f"py{sys.version_info[0]}{sys.version_info[1]}")' 2>/dev/null)
fi
HERMES_DEPS="$ROOT/.test-deps-${HERMES_TAG:-py314}"
KEEP=0
INSTALL=1
MODE=run
ARGS=()

for arg in "$@"; do
  case "$arg" in
    -h|--help) MODE=help ;;
    --all) MODE=all ;;
    --pytest) MODE=pytest ;;
    --list) MODE=list ;;
    --keep) KEEP=1 ;;
    --no-install) INSTALL=0 ;;
    -*) echo "未知选项: $arg（-h 看用法）" >&2; exit 2 ;;
    *) ARGS+=("$arg") ;;
  esac
done

usage() {
  awk 'NR>1 && /^set -/ {exit} NR>1 {sub(/^# ?/, ""); print}' "${BASH_SOURCE[0]}"
}

if [ "$MODE" = help ]; then
  usage
  exit 0
fi

deps_ok() {
  "$PY" - <<'PYCODE' >/dev/null 2>&1
import brotli, zstandard, pytest
PYCODE
}

hermes_deps_ok() {
  [ -x "$HERMES_PY" ] || return 1
  env PYTHONPATH="$HERMES_DEPS" "$HERMES_PY" - <<'PYCODE' >/dev/null 2>&1
import brotli, zstandard
PYCODE
}

ensure_hermes_deps() {
  hermes_deps_ok && return 0
  if [ "$INSTALL" = 1 ] && command -v uv >/dev/null 2>&1; then
    echo "== 为 Hermes 运行时准备 brotli / zstandard（$HERMES_TAG，装到 $HERMES_DEPS）"
    uv pip install --python "$HERMES_PY" --target "$HERMES_DEPS" brotli zstandard || return 1
    hermes_deps_ok && return 0
  fi
  echo "Hermes 运行时缺 brotli / zstandard，且无法自动准备" >&2
  echo "可手动执行 uv pip install --python $HERMES_PY --target $HERMES_DEPS brotli zstandard" >&2
  return 1
}

# ---- 环境 ----
if [ ! -x "$PY" ]; then
  if [ "$PY" != "$DEFAULT_PY" ]; then
    echo "找不到解释器 $PY（由 PYTHON 指定）" >&2
    exit 2
  fi
  command -v uv >/dev/null 2>&1 || {
    echo "仓库没有 .venv，也没有 uv。安装 uv 后重试，或设置 PYTHON=/path/to/python" >&2
    exit 2
  }
  echo "== 仓库没有 .venv，用 uv 创建（Python 3.11）"
  uv venv --python 3.11 "$ROOT/.venv" || exit 2
  PY="$DEFAULT_PY"
fi

if ! deps_ok; then
  if [ "$INSTALL" = 1 ] && [ "$PY" = "$DEFAULT_PY" ]; then
    command -v uv >/dev/null 2>&1 || {
      echo "$PY 缺 brotli / zstandard / pytest，且没有 uv 可安装" >&2
      exit 2
    }
    echo "== 项目 .venv 缺测试依赖，用 uv 补齐"
    uv pip install --python "$PY" -r "$ROOT/requirements.txt" pytest || exit 2
    deps_ok || { echo "依赖安装后仍无法导入，检查 requirements.txt 与网络" >&2; exit 2; }
  else
    echo "解释器 $PY 缺 brotli / zstandard / pytest" >&2
    echo "用默认入口 test/run.sh（自动选 .venv），或自行安装后重试" >&2
    exit 2
  fi
fi

if [ "$MODE" = list ]; then
  cd "$ROOT/test" || exit 2
  for f in ./*.py ./*.js; do
    [ -e "$f" ] || continue
    name=${f#./}
    if grep -q 'agent\.chat_completion_helpers' "$f"; then
      echo "$name  [Hermes 运行时]"
    else
      echo "$name"
    fi
  done | sort
  exit 0
fi

# ---- 隔离副本 ----
BOX=$(mktemp -d "${TMPDIR:-/tmp}/apipool-tests.XXXXXX") || exit 2
cleanup() {
  if [ "$KEEP" = 1 ]; then
    echo "隔离副本保留在 $BOX"
  else
    rm -rf "$BOX"
  fi
}
trap cleanup EXIT

mkdir -p "$BOX/test" "$BOX/db" "$BOX/test-hermes"
cp "$ROOT/api_pool_server.py" "$ROOT/credential_redaction.py" "$BOX/" || exit 2
cp -R "$ROOT/static" "$BOX/" || exit 2
cp "$ROOT"/test/*.py "$ROOT"/test/*.js "$BOX/test/" || exit 2

# 需要 Hermes 运行时的契约测试单独放：它们导入 agent.*，必须用 Hermes 自己的解释器跑。
for f in "$BOX"/test/*.py; do
  if grep -q 'agent\.chat_completion_helpers' "$f"; then
    mv "$f" "$BOX/test-hermes/"
  fi
done

export API_POOL_DB_DIR="$BOX/db"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$BOX${PYTHONPATH:+:$PYTHONPATH}"
cd "$BOX" || exit 2

echo "== 解释器 $("$PY" -c 'import sys; print(sys.executable, sys.version.split()[0])')"
echo "== 隔离副本 $BOX"

RESULTS=()
FAILED=0

run_cmd() {
  local label="$1"; shift
  echo
  echo "-- $label"
  "$@"
  local rc=$?
  if [ $rc -eq 0 ]; then
    RESULTS+=("PASS  $label")
  else
    RESULTS+=("FAIL  $label (exit $rc)")
    FAILED=$((FAILED + 1))
  fi
}

run_py() {
  local base="$1"
  if [ -f "$BOX/test-hermes/$base" ]; then
    if ensure_hermes_deps; then
      run_cmd "$base (Hermes 运行时)" env PYTHONPATH="$BOX:$HERMES_DEPS" "$HERMES_PY" -B "test-hermes/$base"
    else
      RESULTS+=("FAIL  $base (Hermes 运行时不可用)")
      FAILED=$((FAILED + 1))
    fi
  elif grep -qE '^[[:space:]]*(import unittest|from unittest)' "$ROOT/test/$base"; then
    run_cmd "$base" "$PY" -B -m unittest discover -s test -p "$base" -v
  elif grep -qE '^[[:space:]]*def test_' "$ROOT/test/$base"; then
    run_cmd "$base" "$PY" -B -m pytest -q "test/$base"
  else
    run_cmd "$base" "$PY" -B "test/$base"
  fi
}

resolve() {
  local arg="$1" pat f
  local pats=("$ROOT/test/$arg")
  case "$arg" in
    *.*) ;;
    *) pats+=("$ROOT/test/$arg.py" "$ROOT/test/$arg.js") ;;
  esac
  for pat in "${pats[@]}"; do
    while IFS= read -r f; do
      if [ -n "$f" ] && [ -f "$f" ]; then
        echo "$f"
        return 0
      fi
    done < <(compgen -G "$pat")
  done
  return 1
}

if [ "$MODE" = run ]; then
  if [ "${#ARGS[@]}" -eq 0 ]; then
    usage
    exit 2
  fi
  for arg in "${ARGS[@]}"; do
    if ! file=$(resolve "$arg"); then
      echo "找不到测试文件: $arg（--list 看可用文件）" >&2
      exit 2
    fi
    base=$(basename "$file")
    case "$base" in
      *.js) run_cmd "$base" node "test/$base" ;;
      *.py) run_py "$base" ;;
      *) echo "不支持的文件类型: $base" >&2; exit 2 ;;
    esac
  done
fi

if [ "$MODE" = pytest ]; then
  for f in "$ROOT"/test/test_*.py; do
    base=$(basename "$f")
    if grep -qE '^[[:space:]]*def test_' "$f" && ! grep -qE '^[[:space:]]*(import unittest|from unittest)' "$f"; then
      run_cmd "$base" "$PY" -B -m pytest -q "test/$base"
    fi
  done
fi

if [ "$MODE" = all ]; then
  run_cmd "unittest 全量" "$PY" -B -m unittest discover -s test -p 'test_*.py'
  for f in "$ROOT"/test/test_*.py; do
    base=$(basename "$f")
    if grep -qE '^[[:space:]]*def test_' "$f" && ! grep -qE '^[[:space:]]*(import unittest|from unittest)' "$f"; then
      run_cmd "$base" "$PY" -B -m pytest -q "test/$base"
    fi
  done
  for f in "$ROOT"/test/*.js; do
    base=$(basename "$f")
    run_cmd "$base" node "test/$base"
  done
  for f in "$BOX"/test-hermes/*.py; do
    [ -e "$f" ] || continue
    base=$(basename "$f")
    if ensure_hermes_deps; then
      run_cmd "$base (Hermes 运行时)" env PYTHONPATH="$BOX:$HERMES_DEPS" "$HERMES_PY" -B "test-hermes/$base"
    else
      RESULTS+=("FAIL  $base (Hermes 运行时不可用)")
      FAILED=$((FAILED + 1))
    fi
  done
  if [ -f "$ROOT/test/test_gemini_bridge.py" ]; then
    run_cmd "test_gemini_bridge.py" "$PY" -B test/test_gemini_bridge.py
  fi
fi

echo
echo "== 结果"
if [ "${#RESULTS[@]}" -eq 0 ]; then
  echo "没有选中任何测试（检查 --list 与文件名）"
  exit 2
fi
printf '%s\n' "${RESULTS[@]}"
if [ "$FAILED" -gt 0 ]; then
  echo "失败 $FAILED 项"
  exit 1
fi
echo "全部通过"
