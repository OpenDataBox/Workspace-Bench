#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT/logs/env_issue_reruns"
PID_DIR="$LOG_DIR/pids"

RUNS=(
  "Codex--GPT-5.4--Test-Rubrics-Checked"
  "Codex--Gemini-3.1-Pro--Test-Rubrics-Checked"
  "Codex--Kimi-K2.5--Test-Rubrics-Checked"
  "Codex--MiniMax-M2.7--Test-Rubrics-Checked"
  "Codex--GLM-5.1--Test-Rubrics-Checked"
  "Codex--Grok-4.3--Test-Rubrics-Checked"
  "Codex--Qwen-3.6--Test-Rubrics-Checked"
)

cd "$ROOT"
mkdir -p "$LOG_DIR" "$PID_DIR"

log() {
  printf '[%s] %s\n' "$(date '+%F %T')" "$*"
}

existing_runner_pids() {
  pgrep -f 'python3 -u src/agent_runner.py --run-config runs/Codex--.*--Test-Rubrics-Checked--EnvIssueRerun\.yaml' || true
}

existing_codex_pids() {
  pgrep -f 'codex .*output/Codex--.*--Test-Rubrics-Checked--EnvIssueRerun' || true
}

runner_pid_for_run() {
  local run="$1"
  pgrep -f "python3 -u src/agent_runner.py --run-config runs/${run}--EnvIssueRerun\\.yaml" | head -n 1 || true
}

status() {
  for run in "${RUNS[@]}"; do
    local pid
    pid="$(runner_pid_for_run "$run")"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      log "$run EnvIssueRerun running pid=$pid log=$LOG_DIR/$run.log"
    else
      log "$run EnvIssueRerun not running log=$LOG_DIR/$run.log"
    fi
  done
  local runners codexes
  runners="$(existing_runner_pids | xargs echo || true)"
  codexes="$(existing_codex_pids | xargs echo || true)"
  [[ -n "$runners" ]] && log "matching runner pids: $runners"
  [[ -n "$codexes" ]] && log "matching codex child pids: $codexes"
}

stop() {
  log "stopping Codex EnvIssueRerun processes"
  pkill -TERM -f 'python3 -u src/agent_runner.py --run-config runs/Codex--.*--Test-Rubrics-Checked--EnvIssueRerun\.yaml' || true
  pkill -TERM -f 'codex .*output/Codex--.*--Test-Rubrics-Checked--EnvIssueRerun' || true
  sleep 5
  if [[ -n "$(existing_runner_pids)$(existing_codex_pids)" ]]; then
    pkill -KILL -f 'python3 -u src/agent_runner.py --run-config runs/Codex--.*--Test-Rubrics-Checked--EnvIssueRerun\.yaml' || true
    pkill -KILL -f 'codex .*output/Codex--.*--Test-Rubrics-Checked--EnvIssueRerun' || true
  fi
  status
}

start() {
  local runners codexes
  runners="$(existing_runner_pids | xargs echo || true)"
  codexes="$(existing_codex_pids | xargs echo || true)"
  if [[ -n "$runners" || -n "$codexes" ]]; then
    log "refusing to start because matching rerun processes already exist"
    [[ -n "$runners" ]] && log "runner pids: $runners"
    [[ -n "$codexes" ]] && log "codex child pids: $codexes"
    exit 1
  fi

  python3 scripts/prepare_codex_env_issue_rerun.py | tee "$LOG_DIR/prepare.log"

  local pids=()
  for run in "${RUNS[@]}"; do
    local cfg="runs/${run}--EnvIssueRerun.yaml"
    local log_file="$LOG_DIR/$run.log"
    if [[ ! -f "$cfg" ]]; then
      log "missing config: $cfg"
      exit 1
    fi
    : > "$log_file"
    log "launching $run EnvIssueRerun"
    python3 -u src/agent_runner.py --run-config "$cfg" > "$log_file" 2>&1 &
    local pid=$!
    echo "$pid" > "$PID_DIR/$run.pid"
    pids+=("$pid")
  done

  local rc=0
  for pid in "${pids[@]}"; do
    if ! wait "$pid"; then
      rc=1
      log "runner pid=$pid exited non-zero"
    fi
  done
  log "all EnvIssueRerun runners finished rc=$rc"
  exit "$rc"
}

case "${1:-start}" in
  start) start ;;
  status) status ;;
  stop) stop ;;
  *) echo "Usage: $0 [start|status|stop]" >&2; exit 2 ;;
esac
