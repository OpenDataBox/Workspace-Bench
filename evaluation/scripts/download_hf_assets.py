#!/usr/bin/env python3
import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict


LANGUAGE_ALIASES = {
    "en": "en",
    "cn": "cn",
    "zh": "cn",
}

DATASETS = {
    "lite": ("Workspace-Bench/Workspace-Bench-Lite", "tasks_lite"),
    "full": ("Workspace-Bench/Workspace-Bench", "tasks"),
    "workspaces": ("Workspace-Bench/Workspace-Bench-Workspaces", "filesys"),
}

TASK_LAYOUTS = {
    "lite": {
        "en": ("task_lite_clean_en", "task_lite_clean_en_metadata_table.csv"),
        "cn": ("task_lite_clean_cn", "task_lite_clean_cn_metadata_table.csv"),
    },
    "full": {
        "en": ("task_clean_en", "task_en_metadata_table.csv"),
        "cn": ("task_clean_cn", "task_clean_cn_metadata_table.csv"),
    },
}

WORKSPACE_ARCHIVES = {
    "en": "filesys_en.zip",
    "cn": "filesys_cn.zip",
}
LANGUAGE_MARKER = ".workspace_bench_language"
WORKSPACE_RAW_DIRS = (
    "chanpin_raw",
    "kaifa_raw",
    "research_raw",
    "yunying_raw",
    "houqin_raw",
)
WORKSPACE_EXTRACTED_DIR_CANDIDATES = {
    "chanpin_raw": (
        "chanpin_raw",
        "ProductManager_Workdir",
        "AIProductManager_Workdir",
        "ProductManager",
        "AIProductManager",
        "Product_Manager_Workdir",
        "AI_Product_Manager_Workdir",
        "产品人员_Workdir",
        "产品经理_Workdir",
        "产品人员",
        "产品经理",
    ),
    "kaifa_raw": (
        "kaifa_raw",
        "BackendDeveloper_Workdir",
        "BackendDeveloper",
        "Backend_Developer_Workdir",
        "开发人员_Workdir",
        "后端开发人员_Workdir",
        "开发人员",
        "后端开发人员",
    ),
    "research_raw": (
        "research_raw",
        "Research_Workdir",
        "Researcher_Workdir",
        "Researcher",
        "研究人员_Workdir",
        "研究人员",
    ),
    "yunying_raw": (
        "yunying_raw",
        "OperationsManager_Workdir",
        "OperationsManager",
        "Operations_Manager_Workdir",
        "运营人员_Workdir",
        "运营经理_Workdir",
        "运营人员",
        "运营经理",
    ),
    "houqin_raw": (
        "houqin_raw",
        "LogisticsManager_Workdir",
        "LogisticsManager",
        "Logistics_Manager_Workdir",
        "行政后勤人员_Workdir",
        "行政_后勤人员_Workdir",
        "后勤人员_Workdir",
        "行政后勤人员",
        "行政_后勤人员",
        "后勤人员",
    ),
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


def _normalize_language(language: str) -> str:
    key = str(language or "").strip().lower()
    if key not in LANGUAGE_ALIASES:
        valid = ", ".join(sorted(LANGUAGE_ALIASES))
        raise SystemExit(f"unsupported language: {language!r}; choose one of: {valid}")
    return LANGUAGE_ALIASES[key]


def _normalize_language_value(value: Any) -> str | None:
    key = str(value or "").strip().lower()
    if not key:
        return None
    return LANGUAGE_ALIASES.get(key)


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x20000 <= code <= 0x2A6DF
        or 0x2A700 <= code <= 0x2B73F
        or 0x2B740 <= code <= 0x2B81F
        or 0x2B820 <= code <= 0x2CEAF
    )


def _flatten_language_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_flatten_language_values(item))
        return out
    if isinstance(value, dict):
        out: list[str] = []
        for item in value.values():
            out.extend(_flatten_language_values(item))
        return out
    return []


def _detect_language_from_text(*values: Any) -> str:
    text = "\n".join(part for value in values for part in _flatten_language_values(value))
    cjk = 0
    latin = 0
    for ch in text:
        if _is_cjk(ch):
            cjk += 1
        elif ("a" <= ch <= "z") or ("A" <= ch <= "Z"):
            latin += 1
    denom = cjk + latin
    if cjk >= 8 and denom > 0 and (cjk / denom) >= 0.08:
        return "cn"
    return "en"


def _detect_language_from_task_meta(meta: Dict[str, Any]) -> str:
    return _detect_language_from_text(meta.get("task"), meta.get("rubrics"), meta.get("rubric_types"))


def _warn_language(task_id: str | None, message: str) -> None:
    prefix = f" task={task_id}" if task_id else ""
    print(f"[warning]{prefix} {message}", file=sys.stderr)


def _safe_task_id(row: Dict[str, str]) -> str:
    if row.get("id"):
        return str(row["id"]).strip()
    persona = str(row.get("persona") or "task").strip().lower().replace(" ", "_").replace("/", "_")
    absolute_id = str(row.get("absolute_id") or row.get("index") or "").strip()
    return f"{persona}_{absolute_id}" if absolute_id else persona


def _materialize_csv(csv_path: Path, dst: Path, expected_language: str | None = None) -> int:
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
            _normalize_task_meta(meta, task_id=task_id, expected_language=expected_language)
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


def _normalize_task_meta(
    meta: Dict[str, Any],
    task_id: str | None = None,
    expected_language: str | None = None,
) -> Dict[str, Any]:
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

    detected_language = _detect_language_from_task_meta(meta)
    raw_language = meta.get("language")
    normalized_language = _normalize_language_value(raw_language)
    if raw_language not in (None, "") and normalized_language is None:
        valid = ", ".join(sorted(LANGUAGE_ALIASES))
        raise SystemExit(f"unsupported metadata language: {raw_language!r}; choose one of: {valid}")

    task_label = str(meta.get("id") or task_id or "").strip() or None
    if normalized_language:
        meta["language"] = normalized_language
        if detected_language != normalized_language:
            _warn_language(
                task_label,
                f"metadata language {normalized_language!r} conflicts with content-detected {detected_language!r}; keeping metadata language",
            )
    else:
        meta["language"] = detected_language

    if expected_language is not None:
        expected = _normalize_language(expected_language)
        if meta["language"] != expected:
            _warn_language(
                task_label,
                f"metadata language {meta['language']!r} differs from expected download language {expected!r}",
            )
    return meta


def _normalize_task_metadata_files(dst: Path, expected_language: str | None = None) -> int:
    count = 0
    for meta_path in sorted(dst.glob("*/metadata.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(meta, dict):
            continue
        before = json.dumps(meta, ensure_ascii=False, sort_keys=True)
        _normalize_task_meta(meta, task_id=meta_path.parent.name, expected_language=expected_language)
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


def _read_language_marker(path: Path) -> str | None:
    marker = path / LANGUAGE_MARKER
    try:
        value = marker.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except Exception:
        return None
    return _normalize_language(value) if value else None


def _write_language_marker(path: Path, language: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / LANGUAGE_MARKER).write_text(f"{language}\n", encoding="utf-8")


def _ensure_language_compatible(path: Path, language: str, force: bool, asset_name: str) -> None:
    existing = _read_language_marker(path)
    if existing and existing != language and not force:
        raise SystemExit(
            f"{asset_name} already exists for language '{existing}' under {path}. "
            f"Re-run with --force to replace it with '{language}'."
        )


def _snapshot_download(
    repo_id: str,
    dst: Path,
    revision: str | None,
    max_workers: int,
    allow_patterns: list[str] | None = None,
) -> Path:
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
            max_workers=max_workers,
            allow_patterns=allow_patterns,
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


def _normalize_workspace_layout(dst: Path, force: bool, language: str) -> bool:
    if _workspace_dirs_exist(dst) and not force:
        return True

    extracted_roots = [
        dst,
        dst / f"filesys_{language}",
        dst / "filesys_en",
        dst / "filesys_cn",
    ]
    moved_any = False
    for dst_name, src_names in WORKSPACE_EXTRACTED_DIR_CANDIDATES.items():
        target = dst / dst_name
        if target.exists():
            continue
        for extracted_root in extracted_roots:
            for src_name in src_names:
                source = extracted_root / src_name
                if source.is_dir():
                    shutil.move(str(source), str(target))
                    moved_any = True
                    break
            if target.exists():
                break

    for nested_root in (dst / "filesys_en", dst / "filesys_cn"):
        if nested_root.is_dir() and not any(nested_root.iterdir()):
            nested_root.rmdir()

    if moved_any:
        print(f"[ok] normalized workspace directory names under {dst}")
    return _workspace_dirs_exist(dst)


def _extract_workspace_archive(archive_path: Path, dst: Path, force: bool, language: str) -> None:
    _require_command("unzip")
    if not archive_path.exists():
        raise SystemExit(f"workspace archive not found: {archive_path}")
    if _normalize_workspace_layout(dst, force=False, language=language) and not force:
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
    if not _normalize_workspace_layout(dst, force=force, language=language):
        raise SystemExit(
            "workspace archive extracted, but expected raw workspace directories "
            f"were not found under {dst}"
        )


def download_tasks(
    kind: str,
    eval_root: Path,
    revision: str | None,
    force: bool,
    max_workers: int,
    language: str,
) -> None:
    repo_id, dirname = DATASETS[kind]
    task_dirname, metadata_csv = TASK_LAYOUTS[kind][language]
    dst = eval_root / dirname
    tmp = eval_root / ".generated" / "hf_downloads" / f"{kind}_{language}"
    _ensure_language_compatible(dst, language, force, f"{kind} tasks")
    if force and dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)
    snapshot = _snapshot_download(
        repo_id,
        tmp,
        revision,
        max_workers,
        allow_patterns=[f"{task_dirname}/**", metadata_csv],
    )

    preferred_metadata_root = snapshot / task_dirname
    metadata_root = (
        preferred_metadata_root
        if _has_metadata_dirs(preferred_metadata_root)
        else _find_metadata_root(snapshot)
    )
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
        count = _normalize_task_metadata_files(dst, expected_language=language)
        _write_language_marker(dst, language)
        print(f"[ok] downloaded {count} {language} {kind} task directories to {dst}")
        return

    preferred_csv = snapshot / metadata_csv
    csv_path = preferred_csv if preferred_csv.is_file() else _find_csv(snapshot)
    if not csv_path:
        raise SystemExit(f"No task directories or CSV file found in {snapshot}")
    count = _materialize_csv(csv_path, dst, expected_language=language)
    _write_language_marker(dst, language)
    print(f"[ok] materialized {count} {language} {kind} metadata files under {dst}")


def download_workspaces(eval_root: Path, revision: str | None, force: bool, language: str) -> None:
    repo_id, dirname = DATASETS["workspaces"]
    dst = eval_root / dirname
    archive_name = WORKSPACE_ARCHIVES[language]
    archive_path = dst / archive_name
    _ensure_language_compatible(dst, language, force, "workspace filesystems")
    if _normalize_workspace_layout(dst, force=False, language=language) and not force:
        print(f"[ok] workspace filesystems already exist under {dst}")
        _write_language_marker(dst, language)
        return
    dst.mkdir(parents=True, exist_ok=True)
    url = _hf_dataset_url(repo_id, archive_name, revision)
    _download_with_wget(url, archive_path, force)
    print(f"[ok] downloaded workspace archive to {archive_path}")
    _extract_workspace_archive(archive_path, dst, force, language)
    _write_language_marker(dst, language)
    print(f"[ok] extracted {language} workspace filesystems to {dst}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Download Workspace-Bench assets from Hugging Face.")
    parser.add_argument("--lite", action="store_true", help="Download/materialize Workspace-Bench-Lite tasks.")
    parser.add_argument("--full", action="store_true", help="Download/materialize full Workspace-Bench tasks.")
    parser.add_argument("--workspaces", action="store_true", help="Download workspace filesystem assets.")
    parser.add_argument("--all", action="store_true", help="Download all task and workspace assets.")
    parser.add_argument(
        "--language",
        "--lang",
        choices=sorted(LANGUAGE_ALIASES),
        default="en",
        help="Dataset language to download: en for English or cn/zh for Chinese. Defaults to en.",
    )
    parser.add_argument("--revision", default=None, help="Optional Hugging Face dataset revision.")
    parser.add_argument("--force", action="store_true", help="Replace existing target directories.")
    parser.add_argument(
        "--max-workers",
        type=int,
        default=int(os.environ.get("HF_HUB_MAX_WORKERS", "8")),
        help="Maximum parallel Hugging Face downloads for task snapshots.",
    )
    parser.add_argument(
        "--eval-root",
        default=os.environ.get("WORKSPACE_BENCH_EVAL_ROOT") or os.environ.get("RIP_BENCH_EVAL_ROOT") or ".",
        help="Evaluation directory. Defaults to current directory or *_EVAL_ROOT.",
    )
    args = parser.parse_args()

    eval_root = Path(args.eval_root).resolve()
    if not (eval_root / "runs").exists():
        raise SystemExit(f"evaluation root not found: {eval_root}")

    language = _normalize_language(args.language)
    if args.all or args.lite:
        download_tasks("lite", eval_root, args.revision, args.force, args.max_workers, language)
    if args.all or args.full:
        download_tasks("full", eval_root, args.revision, args.force, args.max_workers, language)
    if args.all or args.workspaces:
        download_workspaces(eval_root, args.revision, args.force, language)
    if not (args.all or args.lite or args.full or args.workspaces):
        parser.error("choose at least one of --lite, --full, --workspaces, or --all")


if __name__ == "__main__":
    main()
