#!/usr/bin/env python3
import argparse
import csv
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict


DATASETS = {
    "lite": ("Workspace-Bench/Workspace-Bench-Lite", "tasks_lite"),
    "full": ("Workspace-Bench/Workspace-Bench", "tasks"),
    "workspaces": ("Workspace-Bench/Workspace-Bench-Workspaces", "filesys"),
}

WORKSPACE_ARCHIVE = "filesys_en_workdirs.zip"
WORKSPACE_RAW_DIRS = (
    "chanpin_raw",
    "kaifa_raw",
    "research_raw",
    "yunying_raw",
    "houqin_raw",
)
WORKSPACE_EXTRACTED_DIR_MAP = {
    "ProductManager_Workdir": "chanpin_raw",
    "BackendDeveloper_Workdir": "kaifa_raw",
    "Research_Workdir": "research_raw",
    "OperationsManager_Workdir": "yunying_raw",
    "LogisticsManager_Workdir": "houqin_raw",
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
            _normalize_task_meta(meta, task_id=task_id)
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


def _normalize_task_meta(meta: Dict[str, Any], task_id: str | None = None) -> Dict[str, Any]:
    if task_id:
        meta.setdefault("id", task_id)
    elif not meta.get("id"):
        absolute_id = str(meta.get("absolute_id") or "").strip()
        if absolute_id:
            meta["id"] = absolute_id

    persona = str(meta.get("persona") or meta.get("job") or "").strip()
    file_system = str(meta.get("file_system") or "").strip()
    normalized_fs = PERSONA_TO_FILE_SYSTEM.get(persona) or PERSONA_TO_FILE_SYSTEM.get(file_system) or file_system
    if normalized_fs:
        meta["file_system"] = normalized_fs
        meta.setdefault("user_profit", normalized_fs)
    if persona:
        meta.setdefault("job", persona)
    return meta


def _normalize_task_metadata_files(dst: Path) -> int:
    count = 0
    for meta_path in sorted(dst.glob("*/metadata.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(meta, dict):
            continue
        before = json.dumps(meta, ensure_ascii=False, sort_keys=True)
        _normalize_task_meta(meta, task_id=meta_path.parent.name)
        after = json.dumps(meta, ensure_ascii=False, sort_keys=True)
        if after != before:
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        count += 1
    return count


def _has_metadata_dirs(path: Path) -> bool:
    return any(path.glob("*/metadata.json"))


def _find_metadata_root(path: Path) -> Path | None:
    if _has_metadata_dirs(path):
        return path
    for metadata_path in sorted(path.rglob("metadata.json")):
        parent = metadata_path.parent
        if parent.parent == path:
            return path
        if _has_metadata_dirs(parent.parent):
            return parent.parent
    return None


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


def _hf_dataset_url(repo_id: str, filename: str, revision: str | None) -> str:
    try:
        from huggingface_hub import hf_hub_url
    except ImportError as exc:
        raise SystemExit("Please install huggingface_hub first: pip install huggingface_hub") from exc

    return hf_hub_url(
        repo_id=repo_id,
        filename=filename,
        repo_type="dataset",
        revision=revision,
    )


def _require_command(name: str) -> None:
    if shutil.which(name) is None:
        raise SystemExit(f"Required command not found: {name}")


def _download_with_wget(url: str, archive_path: Path, force: bool) -> None:
    _require_command("wget")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if force and archive_path.exists():
        archive_path.unlink()
    subprocess.run(
        [
            "wget",
            "-c",
            "--progress=dot:giga",
            "-O",
            str(archive_path),
            url,
        ],
        check=True,
    )


def _workspace_dirs_exist(dst: Path) -> bool:
    return all((dst / name).is_dir() for name in WORKSPACE_RAW_DIRS)


def _normalize_workspace_layout(dst: Path, force: bool) -> bool:
    if _workspace_dirs_exist(dst) and not force:
        return True

    extracted_roots = [dst, dst / "filesys_en"]
    moved_any = False
    for src_name, dst_name in WORKSPACE_EXTRACTED_DIR_MAP.items():
        target = dst / dst_name
        if target.exists() and force:
            shutil.rmtree(target) if target.is_dir() else target.unlink()
        if target.exists():
            continue
        for extracted_root in extracted_roots:
            source = extracted_root / src_name
            if source.is_dir():
                shutil.move(str(source), str(target))
                moved_any = True
                break

    nested_root = dst / "filesys_en"
    if nested_root.is_dir() and not any(nested_root.iterdir()):
        nested_root.rmdir()

    if moved_any:
        print(f"[ok] normalized workspace directory names under {dst}")
    return _workspace_dirs_exist(dst)


def _extract_workspace_archive(archive_path: Path, dst: Path, force: bool) -> None:
    _require_command("unzip")
    if not archive_path.exists():
        raise SystemExit(f"workspace archive not found: {archive_path}")
    if _normalize_workspace_layout(dst, force=False) and not force:
        print(f"[ok] workspace filesystems already extracted under {dst}")
        return
    for name in WORKSPACE_RAW_DIRS:
        target = dst / name
        if target.exists():
            shutil.rmtree(target) if target.is_dir() else target.unlink()
    subprocess.run(
        [
            "unzip",
            "-q",
            "-o",
            str(archive_path),
            "-d",
            str(dst),
        ],
        check=True,
    )
    if not _normalize_workspace_layout(dst, force=force):
        raise SystemExit(
            "workspace archive extracted, but expected raw workspace directories "
            f"were not found under {dst}"
        )


def download_tasks(kind: str, eval_root: Path, revision: str | None, force: bool) -> None:
    repo_id, dirname = DATASETS[kind]
    dst = eval_root / dirname
    tmp = eval_root / ".generated" / "hf_downloads" / kind
    if force and dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)
    snapshot = _snapshot_download(repo_id, tmp, revision)

    metadata_root = _find_metadata_root(snapshot)
    if metadata_root is not None:
        for child in metadata_root.iterdir():
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
        count = _normalize_task_metadata_files(dst)
        print(f"[ok] downloaded {count} {kind} task directories to {dst}")
        return

    csv_path = _find_csv(snapshot)
    if not csv_path:
        raise SystemExit(f"No task directories or CSV file found in {snapshot}")
    count = _materialize_csv(csv_path, dst)
    print(f"[ok] materialized {count} {kind} metadata files under {dst}")


def download_workspaces(eval_root: Path, revision: str | None, force: bool) -> None:
    repo_id, dirname = DATASETS["workspaces"]
    dst = eval_root / dirname
    archive_path = dst / WORKSPACE_ARCHIVE
    if _normalize_workspace_layout(dst, force=False) and not force:
        print(f"[ok] workspace filesystems already exist under {dst}")
        return
    dst.mkdir(parents=True, exist_ok=True)
    url = _hf_dataset_url(repo_id, WORKSPACE_ARCHIVE, revision)
    _download_with_wget(url, archive_path, force)
    print(f"[ok] downloaded workspace archive to {archive_path}")
    _extract_workspace_archive(archive_path, dst, force)
    print(f"[ok] extracted workspace filesystems to {dst}")


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
