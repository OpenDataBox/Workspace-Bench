#!/usr/bin/env python3
import argparse
import csv
import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict


DATASETS = {
    "lite": ("Workspace-Bench/Workspace-Bench-Lite", "tasks_lite"),
    "full": ("Workspace-Bench/Workspace-Bench", "tasks"),
    "workspaces": ("Workspace-Bench/Workspace-Bench-Workspaces", "filesys"),
}

PERSONA_TO_FILE_SYSTEM = {
    "Product Manager": "产品人员",
    "Backend Developer": "开发人员",
    "Researcher": "研究人员",
    "Operations Manager": "运营人员",
    "Logistics Manager": "行政/后勤人员",
}


def _load_jsonish(value: str) -> Any:
    s = str(value or "").strip()
    if not s:
        return None
    if s[:1] not in "[{":
        return value
    try:
        return json.loads(s)
    except Exception:
        return value


def _safe_task_id(row: Dict[str, str]) -> str:
    if row.get("id"):
        return str(row["id"]).strip()
    persona = str(row.get("persona") or "task").strip().lower().replace(" ", "_").replace("/", "_")
    absolute_id = str(row.get("absolute_id") or row.get("index") or "").strip()
    return f"{persona}_{absolute_id}" if absolute_id else persona


def _materialize_csv(csv_path: Path, dst: Path) -> int:
    count = 0
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            task_id = _safe_task_id(row)
            persona = str(row.get("persona") or "").strip()
            meta: Dict[str, Any] = {}
            for key, value in row.items():
                if value is None:
                    continue
                parsed = _load_jsonish(value)
                if parsed not in ("", None):
                    meta[key] = parsed
            meta.setdefault("id", task_id)
            meta.setdefault("file_system", PERSONA_TO_FILE_SYSTEM.get(persona, persona))
            meta.setdefault("job", persona)
            meta.setdefault("user_profit", meta["file_system"])
            if "output_files" not in meta and "output_file" in meta:
                meta["output_files"] = [meta["output_file"]]

            task_dir = dst / task_id
            task_dir.mkdir(parents=True, exist_ok=True)
            (task_dir / "metadata.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            count += 1
    return count


def _has_metadata_dirs(path: Path) -> bool:
    return any(path.glob("*/metadata.json"))


def _find_csv(path: Path) -> Path | None:
    candidates = sorted(path.rglob("*.csv"))
    return candidates[0] if candidates else None


def _snapshot_download(repo_id: str, dst: Path, revision: str | None) -> Path:
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise SystemExit("Please install huggingface_hub first: pip install huggingface_hub") from exc

    dst.mkdir(parents=True, exist_ok=True)
    return Path(
        snapshot_download(
            repo_id=repo_id,
            repo_type="dataset",
            revision=revision,
            local_dir=str(dst),
            local_dir_use_symlinks=False,
        )
    )


def download_tasks(kind: str, eval_root: Path, revision: str | None, force: bool) -> None:
    repo_id, dirname = DATASETS[kind]
    dst = eval_root / dirname
    tmp = eval_root / ".generated" / "hf_downloads" / kind
    if force and dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)
    snapshot = _snapshot_download(repo_id, tmp, revision)

    if _has_metadata_dirs(snapshot):
        for child in snapshot.iterdir():
            if child.name.startswith("."):
                continue
            target = dst / child.name
            if target.exists():
                if force:
                    shutil.rmtree(target) if target.is_dir() else target.unlink()
                else:
                    continue
            if child.is_dir():
                shutil.copytree(child, target)
            else:
                shutil.copy2(child, target)
        print(f"[ok] downloaded {kind} task directories to {dst}")
        return

    csv_path = _find_csv(snapshot)
    if not csv_path:
        raise SystemExit(f"No task directories or CSV file found in {snapshot}")
    count = _materialize_csv(csv_path, dst)
    print(f"[ok] materialized {count} {kind} metadata files under {dst}")


def download_workspaces(eval_root: Path, revision: str | None, force: bool) -> None:
    repo_id, dirname = DATASETS["workspaces"]
    dst = eval_root / dirname
    if force and dst.exists():
        shutil.rmtree(dst)
    _snapshot_download(repo_id, dst, revision)
    print(f"[ok] downloaded workspace filesystems to {dst}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Download Workspace-Bench assets from Hugging Face.")
    parser.add_argument("--lite", action="store_true", help="Download/materialize Workspace-Bench-Lite tasks.")
    parser.add_argument("--full", action="store_true", help="Download/materialize full Workspace-Bench tasks.")
    parser.add_argument("--workspaces", action="store_true", help="Download workspace filesystem assets.")
    parser.add_argument("--all", action="store_true", help="Download all task and workspace assets.")
    parser.add_argument("--revision", default=None, help="Optional Hugging Face dataset revision.")
    parser.add_argument("--force", action="store_true", help="Replace existing target directories.")
    parser.add_argument(
        "--eval-root",
        default=os.environ.get("WORKSPACE_BENCH_EVAL_ROOT") or os.environ.get("RIP_BENCH_EVAL_ROOT") or ".",
        help="Evaluation directory. Defaults to current directory or *_EVAL_ROOT.",
    )
    args = parser.parse_args()

    eval_root = Path(args.eval_root).resolve()
    if not (eval_root / "runs").exists():
        raise SystemExit(f"evaluation root not found: {eval_root}")

    if args.all or args.lite:
        download_tasks("lite", eval_root, args.revision, args.force)
    if args.all or args.full:
        download_tasks("full", eval_root, args.revision, args.force)
    if args.all or args.workspaces:
        download_workspaces(eval_root, args.revision, args.force)
    if not (args.all or args.lite or args.full or args.workspaces):
        parser.error("choose at least one of --lite, --full, --workspaces, or --all")


if __name__ == "__main__":
    main()
