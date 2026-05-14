#!/usr/bin/env python3
import csv
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT.parents[1] / "codex_python_env_issues.csv"
TASKS_ROOT = ROOT / "tasks_lite"
OUTPUT_ROOT = ROOT / "output"
RUNS_ROOT = ROOT / "runs"
TMP_ROOT = ROOT / "tmp_tasks" / "codex_env_issue_rerun"
ARCHIVE_ROOT = OUTPUT_ROOT / "_archive"


def _copy_task_slice(run: str, task_ids: list[str]) -> Path:
    dst_root = TMP_ROOT / run
    if dst_root.exists():
        shutil.rmtree(dst_root)
    dst_root.mkdir(parents=True, exist_ok=True)
    for task_id in task_ids:
        src = TASKS_ROOT / task_id
        if src.is_dir():
            shutil.copytree(src, dst_root / task_id)
    return dst_root


def _make_rerun_config(run: str, task_root: Path) -> Path:
    src = RUNS_ROOT / f"{run}.yaml"
    dst = RUNS_ROOT / f"{run}--EnvIssueRerun.yaml"
    text = src.read_text(encoding="utf-8")
    text = text.replace("run_name: Test-Rubrics-Checked", "run_name: Test-Rubrics-Checked--EnvIssueRerun")
    lines = []
    for line in text.splitlines():
        if line.startswith("task_path:"):
            lines.append(f"task_path: {task_root}")
        else:
            lines.append(line)
    dst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dst


def main() -> None:
    by_run: dict[str, set[str]] = {}
    with CSV_PATH.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            run = (row.get("setting_name") or "").strip()
            task_id = (row.get("task_id") or "").strip()
            if run and task_id:
                by_run.setdefault(run, set()).add(task_id)

    ts = time.strftime("%Y%m%d_%H%M%S")
    archive = ARCHIVE_ROOT / f"codex_python_env_issues_rerun_{ts}"
    archive.mkdir(parents=True, exist_ok=True)

    moved = 0
    created = []
    for run in sorted(by_run):
        task_ids = sorted(by_run[run])
        for task_id in task_ids:
            src = OUTPUT_ROOT / run / task_id
            if src.exists():
                dst = archive / run / task_id
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                moved += 1
        task_root = _copy_task_slice(run, task_ids)
        cfg = _make_rerun_config(run, task_root)
        created.append((run, len(task_ids), cfg))

    print(f"archive={archive}")
    print(f"archived_case_dirs={moved}")
    for run, count, cfg in created:
        print(f"{run}: tasks={count} config={cfg}")


if __name__ == "__main__":
    main()
