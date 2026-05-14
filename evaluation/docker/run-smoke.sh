#!/usr/bin/env bash
set -euo pipefail

ROOT="${WORKSPACE_BENCH_ROOT:-${RIP_BENCH_ROOT:-/workspace/Workspace-Bench}}"
EVAL_ROOT="${WORKSPACE_BENCH_EVAL_ROOT:-${RIP_BENCH_EVAL_ROOT:-$ROOT/evaluation}}"

RUNS=(
  "Codex--Kimi-K2.5--DockerSmoke.yaml"
  "OpenClaw--Kimi-K2.5--DockerSmoke.yaml"
  "DeepAgent--Kimi-K2.5--DockerSmoke.yaml"
)

if [[ "${WORKSPACE_BENCH_SMOKE_INCLUDE_CLAUDECODE:-${RIP_BENCH_SMOKE_INCLUDE_CLAUDECODE:-0}}" == "1" ]]; then
  RUNS+=("ClaudeCode--GLM-5.1--DockerSmoke.yaml")
fi

prepare_args=(--repo-root "$ROOT")
if [[ "${WORKSPACE_BENCH_ENSURE_WORKDIRS:-${RIP_BENCH_ENSURE_WORKDIRS:-0}}" == "1" ]]; then
  prepare_args+=(--ensure-workdirs)
fi

python3 "$EVAL_ROOT/scripts/prepare_docker_paths.py" "${prepare_args[@]}"

cd "$EVAL_ROOT"
for run_config in "${RUNS[@]}"; do
  echo "[smoke] $run_config"
  python3 -u src/agent_runner.py --run-config ".generated/docker/runs/$run_config"
  run_name="${run_config%.yaml}"
  python3 scripts/assert_agent_runner_report.py "output/${run_name}/agent_runner_report.json"
done

echo "[ok] docker smoke finished"
