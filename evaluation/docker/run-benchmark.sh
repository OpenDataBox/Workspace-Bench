#!/usr/bin/env bash
set -euo pipefail

ROOT="${WORKSPACE_BENCH_ROOT:-${RIP_BENCH_ROOT:-/workspace/Workspace-Bench}}"
EVAL_ROOT="${WORKSPACE_BENCH_EVAL_ROOT:-${RIP_BENCH_EVAL_ROOT:-$ROOT/evaluation}}"
RUN_CONFIG_INPUT="${1:-Codex--GLM-5.1--Test-Rubrics-Checked.yaml}"

if [[ "$RUN_CONFIG_INPUT" == */* ]]; then
  RUN_CONFIG_NAME="$(basename "$RUN_CONFIG_INPUT")"
else
  RUN_CONFIG_NAME="$RUN_CONFIG_INPUT"
fi

prepare_args=(--repo-root "$ROOT")
if [[ "${WORKSPACE_BENCH_ENSURE_WORKDIRS:-${RIP_BENCH_ENSURE_WORKDIRS:-0}}" == "1" ]]; then
  prepare_args+=(--ensure-workdirs)
fi

python3 "$EVAL_ROOT/scripts/prepare_docker_paths.py" "${prepare_args[@]}"

RUN_CONFIG="$EVAL_ROOT/.generated/docker/runs/$RUN_CONFIG_NAME"

if [[ ! -f "$RUN_CONFIG" ]]; then
  echo "[error] run config not found: $RUN_CONFIG" >&2
  exit 1
fi

cd "$EVAL_ROOT"
python3 -u src/agent_runner.py --run-config "$RUN_CONFIG"
