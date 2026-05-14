#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT/logs/full_runs"
PID_DIR="$LOG_DIR/pids"
ARCHIVE_DIR="$ROOT/output/_archive"

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

is_running_pid() {
  local pid="$1"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

existing_runner_pids() {
  pgrep -f 'python3 -u src/agent_runner.py --run-config runs/Codex--(GPT-5\.4|Gemini-3\.1-Pro|Kimi-K2\.5|MiniMax-M2\.7|GLM-5\.1|Grok-4\.3|Qwen-3\.6)--Test-Rubrics-Checked\.yaml' || true
}

runner_pid_for_run() {
  local run="$1"
  pgrep -f "python3 -u src/agent_runner.py --run-config runs/${run}\\.yaml" | head -n 1 || true
}

existing_codex_pids() {
  pgrep -f 'codex .*output/Codex--(GPT-5\.4|Gemini-3\.1-Pro|Kimi-K2\.5|MiniMax-M2\.7|GLM-5\.1|Grok-4\.3|Qwen-3\.6)--Test-Rubrics-Checked' || true
}

status() {
  local any=0
  for run in "${RUNS[@]}"; do
    local pid_file="$PID_DIR/$run.pid"
    local pid=""
    [[ -f "$pid_file" ]] && pid="$(cat "$pid_file" 2>/dev/null || true)"
    if ! is_running_pid "$pid"; then
      pid="$(runner_pid_for_run "$run")"
      [[ -n "$pid" ]] && echo "$pid" > "$pid_file"
    fi
    if is_running_pid "$pid"; then
      log "$run runner running pid=$pid log=$LOG_DIR/$run.log"
      any=1
    else
      log "$run runner not running log=$LOG_DIR/$run.log"
    fi
  done
  local runners codexes
  runners="$(existing_runner_pids | xargs echo || true)"
  codexes="$(existing_codex_pids | xargs echo || true)"
  [[ -n "$runners" ]] && log "matching runner pids: $runners"
  [[ -n "$codexes" ]] && log "matching codex child pids: $codexes"
  return 0
}

stop() {
  log "stopping matching Codex full-run processes"
  pkill -TERM -f 'python3 -u src/agent_runner.py --run-config runs/Codex--(GPT-5\.4|Gemini-3\.1-Pro|Kimi-K2\.5|MiniMax-M2\.7|GLM-5\.1|Grok-4\.3|Qwen-3\.6)--Test-Rubrics-Checked\.yaml' || true
  pkill -TERM -f 'codex .*output/Codex--(GPT-5\.4|Gemini-3\.1-Pro|Kimi-K2\.5|MiniMax-M2\.7|GLM-5\.1|Grok-4\.3|Qwen-3\.6)--Test-Rubrics-Checked' || true
  sleep 5
  if [[ -n "$(existing_runner_pids)$(existing_codex_pids)" ]]; then
    log "some processes remain after TERM; sending KILL"
    pkill -KILL -f 'python3 -u src/agent_runner.py --run-config runs/Codex--(GPT-5\.4|Gemini-3\.1-Pro|Kimi-K2\.5|MiniMax-M2\.7|GLM-5\.1|Grok-4\.3|Qwen-3\.6)--Test-Rubrics-Checked\.yaml' || true
    pkill -KILL -f 'codex .*output/Codex--(GPT-5\.4|Gemini-3\.1-Pro|Kimi-K2\.5|MiniMax-M2\.7|GLM-5\.1|Grok-4\.3|Qwen-3\.6)--Test-Rubrics-Checked' || true
  fi
  status
}

archive_existing_outputs() {
  [[ "${ARCHIVE_EXISTING:-0}" == "1" ]] || return 0
  mkdir -p "$ARCHIVE_DIR"
  local ts
  ts="$(date '+%Y%m%d_%H%M%S')"
  for run in "${RUNS[@]}"; do
    if [[ -d "$ROOT/output/$run" ]]; then
      log "archiving output/$run -> output/_archive/${run}.${ts}"
      mv "$ROOT/output/$run" "$ARCHIVE_DIR/${run}.${ts}"
    fi
  done
}

start() {
  local runners codexes
  runners="$(existing_runner_pids | xargs echo || true)"
  codexes="$(existing_codex_pids | xargs echo || true)"
  if [[ -n "$runners" || -n "$codexes" ]]; then
    log "refusing to start because matching processes already exist"
    [[ -n "$runners" ]] && log "runner pids: $runners"
    [[ -n "$codexes" ]] && log "codex child pids: $codexes"
    exit 1
  fi

  archive_existing_outputs

  log "starting Codex full tasks_lite run"
  log "logs: $LOG_DIR"
  local pids=()
  for run in "${RUNS[@]}"; do
    local cfg="runs/$run.yaml"
    local log_file="$LOG_DIR/$run.log"
    if [[ ! -f "$cfg" ]]; then
      log "missing config: $cfg"
      exit 1
    fi
    : > "$log_file"
    log "launching $run"
    python3 -u src/agent_runner.py --run-config "$cfg" > "$log_file" 2>&1 &
    local pid=$!
    echo "$pid" > "$PID_DIR/$run.pid"
    pids+=("$pid")
    log "$run pid=$pid log=$log_file"
  done

  local rc=0
  for pid in "${pids[@]}"; do
    if ! wait "$pid"; then
      rc=1
      log "runner pid=$pid exited non-zero"
    fi
  done
  log "all runners finished rc=$rc"
  exit "$rc"
}

case "${1:-start}" in
  start) start ;;
  status) status ;;
  stop) stop ;;
  *) echo "Usage: $0 [start|status|stop]" >&2; exit 2 ;;
esac
