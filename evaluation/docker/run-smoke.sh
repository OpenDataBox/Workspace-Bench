#!/usr/bin/env bash
set -euo pipefail

ROOT="${WORKSPACE_BENCH_ROOT:-${RIP_BENCH_ROOT:-/workspace/Workspace-Bench}}"
EVAL_ROOT="${WORKSPACE_BENCH_EVAL_ROOT:-${RIP_BENCH_EVAL_ROOT:-$ROOT/evaluation}}"

HARNESSES=(
  "codex"
  "openclaw"
  "deepagent"
  "claudecode"
)

REPORTS=(
  "output/Codex--Kimi-K2.5--Smoke/agent_runner_report.json"
  "output/OpenClaw--Kimi-K2.5--Smoke/agent_runner_report.json"
  "output/DeepAgent--Kimi-K2.5--Smoke/agent_runner_report.json"
  "output/ClaudeCode--Kimi-K2.5--Smoke/agent_runner_report.json"
)

prepare_args=(--repo-root "$ROOT")
if [[ "${WORKSPACE_BENCH_ENSURE_WORKDIRS:-${RIP_BENCH_ENSURE_WORKDIRS:-0}}" == "1" ]]; then
  prepare_args+=(--ensure-workdirs)
fi

python3 "$EVAL_ROOT/scripts/prepare_docker_paths.py" "${prepare_args[@]}"

cd "$EVAL_ROOT"
for i in "${!HARNESSES[@]}"; do
  harness="${HARNESSES[$i]}"
  echo "[smoke] $harness kimi-k2.5"
  run_config="$(python3 scripts/build_run_config.py \
    --eval-root "$EVAL_ROOT" \
    --harness "$harness" \
    --model kimi-k2.5 \
    --dataset smoke)"
  if [[ "${WORKSPACE_BENCH_PREPARE_WORKDIRS:-${RIP_BENCH_PREPARE_WORKDIRS:-1}}" == "1" ]]; then
    python3 scripts/prepare_workdirs_for_run.py --run-config "$run_config"
  fi
  python3 -u src/agent_runner.py --run-config "$run_config"
  python3 scripts/assert_agent_runner_report.py "${REPORTS[$i]}"
done

echo "[ok] docker smoke finished"
