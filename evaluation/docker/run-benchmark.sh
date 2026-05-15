#!/usr/bin/env bash
set -euo pipefail

ROOT="${WORKSPACE_BENCH_ROOT:-${RIP_BENCH_ROOT:-/workspace/Workspace-Bench}}"
EVAL_ROOT="${WORKSPACE_BENCH_EVAL_ROOT:-${RIP_BENCH_EVAL_ROOT:-$ROOT/evaluation}}"

if [[ "$#" -eq 0 ]]; then
  set -- --harness codex --model kimi-k2.5 --dataset lite
fi

if [[ "${1:-}" == --* ]]; then
  cd "$EVAL_ROOT"
  RUN_CONFIG="$(python3 scripts/build_run_config.py --eval-root "$EVAL_ROOT" "$@")"
else
  RUN_CONFIG_INPUT="$1"

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
fi

if [[ ! -f "$RUN_CONFIG" ]]; then
  echo "[error] run config not found: $RUN_CONFIG" >&2
  exit 1
fi

cd "$EVAL_ROOT"
if [[ "${WORKSPACE_BENCH_PREPARE_WORKDIRS:-${RIP_BENCH_PREPARE_WORKDIRS:-1}}" == "1" ]]; then
  python3 scripts/prepare_workdirs_for_run.py --run-config "$RUN_CONFIG"
fi
python3 -u src/agent_runner.py --run-config "$RUN_CONFIG"
