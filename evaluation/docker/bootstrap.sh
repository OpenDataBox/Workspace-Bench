#!/usr/bin/env bash
set -euo pipefail

ROOT="${WORKSPACE_BENCH_ROOT:-${RIP_BENCH_ROOT:-/workspace/Workspace-Bench}}"
EVAL_ROOT="${WORKSPACE_BENCH_EVAL_ROOT:-${RIP_BENCH_EVAL_ROOT:-$ROOT/evaluation}}"

python3 -m pip install -q --break-system-packages -e "$ROOT/deepagents/libs/deepagents"
python3 -m pip install -q --break-system-packages -e "$ROOT/deepagents/libs/cli"

npm install --prefix "$EVAL_ROOT"
npm install --prefix "$EVAL_ROOT/baselines"

prepare_args=(--repo-root "$ROOT")
if [[ "${WORKSPACE_BENCH_ENSURE_WORKDIRS:-${RIP_BENCH_ENSURE_WORKDIRS:-0}}" == "1" ]]; then
  prepare_args+=(--ensure-workdirs)
fi

python3 "$EVAL_ROOT/scripts/prepare_docker_paths.py" "${prepare_args[@]}"

echo "[ok] docker bootstrap finished"
