#!/usr/bin/env python3
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARCHIVE = ROOT / 'output/_archive/quota_failed_20260512_130455'
RUNS = [
    'Codex--GPT-5.4--Test-Rubrics-Checked',
    'Codex--Gemini-3.1-Pro--Test-Rubrics-Checked',
    'Codex--Kimi-K2.5--Test-Rubrics-Checked',
    'Codex--MiniMax-M2.7--Test-Rubrics-Checked',
    'Codex--GLM-5.1--Test-Rubrics-Checked',
    'Codex--Grok-4.3--Test-Rubrics-Checked',
    'Codex--Qwen-3.6--Test-Rubrics-Checked',
]

def main() -> int:
    archive = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ARCHIVE
    if not archive.is_dir():
        print(f'missing archive: {archive}', file=sys.stderr)
        return 2
    restored = {}
    for run in RUNS:
        src_run = archive / run
        dst_run = ROOT / 'output' / run
        count = 0
        if not src_run.is_dir():
            restored[run] = 0
            continue
        dst_run.mkdir(parents=True, exist_ok=True)
        for agent_json in sorted(src_run.glob('*/agent.json')):
            try:
                data = json.loads(agent_json.read_text(encoding='utf-8'))
            except Exception:
                continue
            case_dir = agent_json.parent
            out_dir = case_dir / 'output'
            if data.get('status') != 'passed' or not out_dir.is_dir() or not any(out_dir.iterdir()):
                continue
            target = dst_run / case_dir.name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(case_dir, target, symlinks=True)
            count += 1
        restored[run] = count
    for run, count in restored.items():
        print(f'{run}: restored_passed={count}')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
