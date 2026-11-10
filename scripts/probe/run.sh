#!/usr/bin/env bash
# 核心探针：tests/probes 下所有非可选、非真实 LLM 用例。
# 真实 LLM（real_llm）与可选诊断（optional）默认不跑；需要时显式加 -m。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEFAULT_BIN="/opt/witty-service/opencode-1.17.13/node_modules/opencode-ai/bin/opencode.exe"
BIN="${WITTY_PROBE_OPENCODE_BIN:-$DEFAULT_BIN}"
if [[ ! -f "$BIN" ]]; then
  echo "未找到 OpenCode 1.17.13: $BIN" >&2
  echo "请设置 WITTY_PROBE_OPENCODE_BIN 或 WITTY_PROBE_OPENCODE_DIR" >&2
  exit 2
fi

export WITTY_PROBE_OPENCODE_BIN="$BIN"
export WITTY_PROBE_TMP_ROOT="${WITTY_PROBE_TMP_ROOT:-/root/.cache/witty-probe/pytest}"
mkdir -p "$WITTY_PROBE_TMP_ROOT"

PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi

cd "$ROOT"
exec "$PYTHON" -m pytest tests/probes \
  -m "probe and not real_llm and not optional" "$@"
