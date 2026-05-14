#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TS="$(date '+%Y%m%d_%H%M%S')"
OUT_ARCHIVE="$ROOT/output/_archive/quota_failed_$TS"
LOG_ARCHIVE="$ROOT/logs/full_runs_archive/quota_failed_$TS"
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

log() {
  printf '[%s] %s\n' "$(date '+%F %T')" "$*"
}

log "stopping existing full-run processes"
./scripts/run_codex_full_nohup.sh stop || true

mkdir -p "$OUT_ARCHIVE" "$LOG_ARCHIVE"
for run in "${RUNS[@]}"; do
  if [[ -d "$ROOT/output/$run" ]]; then
    log "archiving output/$run -> $OUT_ARCHIVE/$run"
    mv "$ROOT/output/$run" "$OUT_ARCHIVE/$run"
  fi
  if [[ -f "$ROOT/logs/full_runs/$run.log" ]]; then
    log "archiving logs/full_runs/$run.log -> $LOG_ARCHIVE/$run.log"
    mv "$ROOT/logs/full_runs/$run.log" "$LOG_ARCHIVE/$run.log"
  fi
  if [[ -f "$ROOT/logs/full_runs/pids/$run.pid" ]]; then
    mv "$ROOT/logs/full_runs/pids/$run.pid" "$LOG_ARCHIVE/$run.pid"
  fi
done
if [[ -f "$ROOT/logs/full_runs/launcher.log" ]]; then
  mv "$ROOT/logs/full_runs/launcher.log" "$LOG_ARCHIVE/launcher.log"
fi

log "rolling back Codex workdirs from standard_work_dir"
python3 - <<'PY'
import glob, json, os, sys
sys.path.insert(0, 'src')
from filesys_utils import filesys_rollback
seen=set()
for fs_map in sorted(glob.glob('fs_map/fs_map_Codex_*.json')):
    data=json.load(open(fs_map, encoding='utf-8'))
    standard=data.get('standard_work_dir', {})
    work=data.get('work_dir', {})
    for role, wdir in work.items():
        sdir=standard.get(role)
        if not sdir:
            continue
        key=(os.path.abspath(sdir), os.path.abspath(wdir))
        if key in seen:
            continue
        seen.add(key)
        print(f'rollback {fs_map} {role}: {wdir}')
        filesys_rollback(standard_work_dir=sdir, work_dir=wdir)
print(f'rolled_back={len(seen)}')
PY

mkdir -p "$ROOT/logs/full_runs/pids"
log "recovery complete"
log "archived outputs: $OUT_ARCHIVE"
log "archived logs: $LOG_ARCHIVE"
log "restart with: nohup ./scripts/run_codex_full_nohup.sh start > logs/full_runs/launcher.log 2>&1 &"
