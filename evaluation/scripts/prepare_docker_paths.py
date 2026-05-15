#!/usr/bin/env python3
import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any

import yaml


Json = Any
HOST_PREFIXES = (
    "/home/yukai/project/Workspace-Bench/evaluation",
    "/home/yukai/project/Workspace-Bench/RIP-Bench/evaluation",
    "/home/tangzirui/RIP-Bench/evaluation",
    "/data00/home/tangzirui/RIP-Bench/evaluation",
    "/Users/handsomexu/Desktop/RIP-Bench/evaluation",
)


def _docker_eval_root(repo_root: Path) -> Path:
    return repo_root / "evaluation"


def _normalize_path_string(value: str, eval_root: Path) -> str:
    s = value.strip()
    for prefix in HOST_PREFIXES:
        if s == prefix:
            return str(eval_root)
        if s.startswith(prefix + "/"):
            return str(eval_root / s[len(prefix) + 1 :])
    if s.startswith("${RIP_BENCH_EVAL_ROOT}/"):
        return str(eval_root / s.split("${RIP_BENCH_EVAL_ROOT}/", 1)[1])
    if s == "${RIP_BENCH_EVAL_ROOT}":
        return str(eval_root)
    if s.startswith("${WORKSPACE_BENCH_EVAL_ROOT}/"):
        return str(eval_root / s.split("${WORKSPACE_BENCH_EVAL_ROOT}/", 1)[1])
    if s == "${WORKSPACE_BENCH_EVAL_ROOT}":
        return str(eval_root)
    return s


def _normalize_config(value: Json, eval_root: Path) -> Json:
    if isinstance(value, str):
        return _normalize_path_string(value, eval_root)
    if isinstance(value, list):
        return [_normalize_config(v, eval_root) for v in value]
    if isinstance(value, dict):
        return {k: _normalize_config(v, eval_root) for k, v in value.items()}
    return value


def _resolve_eval_path(path_value: str, eval_root: Path) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    return eval_root / path


def _normalize_fs_path(value: str, eval_root: Path) -> str:
    s = _normalize_path_string(value, eval_root)
    if s.startswith("filesys/"):
        return str(eval_root / s)
    return s


def _normalize_fs_map(value: Json, eval_root: Path) -> Json:
    if isinstance(value, str):
        return _normalize_fs_path(value, eval_root)
    if isinstance(value, list):
        return [_normalize_fs_map(v, eval_root) for v in value]
    if isinstance(value, dict):
        return {k: _normalize_fs_map(v, eval_root) for k, v in value.items()}
    return value


def _ensure_workdirs_from_fsmap(fsmap_obj: dict, eval_root: Path) -> None:
    standard_dirs = fsmap_obj.get("standard_work_dir")
    work_dirs = fsmap_obj.get("work_dir")
    if not isinstance(standard_dirs, dict) or not isinstance(work_dirs, dict):
        return
    for role, work_dir in work_dirs.items():
        standard_dir = standard_dirs.get(role)
        if not isinstance(work_dir, str) or not isinstance(standard_dir, str):
            continue
        work_path = _resolve_eval_path(work_dir, eval_root)
        standard_path = _resolve_eval_path(standard_dir, eval_root)
        if work_path.exists() or not standard_path.exists():
            continue
        shutil.copytree(standard_path, work_path)


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _write_json(path: Path, data: Json) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _clear_generated_files(path: Path, pattern: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for child in path.glob(pattern):
        if child.is_file() or child.is_symlink():
            child.unlink()


def _generate_runs(eval_root: Path, dst_runs: Path) -> None:
    src_runs = eval_root / "runs"
    dst_fs_map_root = eval_root / ".generated" / "docker" / "fs_map"
    _clear_generated_files(dst_runs, "*.yaml")
    for src in sorted(src_runs.glob("*.yaml")):
        data = yaml.safe_load(src.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            continue
        data = _normalize_config(data, eval_root)
        fs_map_file = data.get("fs_map_file")
        if isinstance(fs_map_file, str) and fs_map_file.strip():
            data["fs_map_file"] = str(dst_fs_map_root / Path(fs_map_file).name)
        if data.get("eval_while_running") is True and data.get("run_name") == "Smoke":
            data["eval_while_running"] = False
        _write_yaml(dst_runs / src.name, data)


def _generate_fs_maps(eval_root: Path, dst_fs_maps: Path, ensure_workdirs: bool) -> None:
    src_fs_maps = eval_root / "fs_map"
    _clear_generated_files(dst_fs_maps, "*.json")
    for src in sorted(src_fs_maps.glob("*.json")):
        obj = json.loads(src.read_text(encoding="utf-8"))
        obj = _normalize_fs_map(obj, eval_root)
        _write_json(dst_fs_maps / src.name, obj)
        if ensure_workdirs and isinstance(obj, dict):
            _ensure_workdirs_from_fsmap(obj, eval_root)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--ensure-workdirs", action="store_true")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    eval_root = _docker_eval_root(repo_root)
    if not eval_root.exists():
        raise SystemExit(f"evaluation root not found: {eval_root}")

    os.environ.setdefault("WORKSPACE_BENCH_ROOT", str(repo_root))
    os.environ.setdefault("WORKSPACE_BENCH_EVAL_ROOT", str(eval_root))
    os.environ.setdefault("RIP_BENCH_ROOT", os.environ["WORKSPACE_BENCH_ROOT"])
    os.environ.setdefault("RIP_BENCH_EVAL_ROOT", os.environ["WORKSPACE_BENCH_EVAL_ROOT"])

    dst_root = eval_root / ".generated" / "docker"
    _generate_fs_maps(eval_root, dst_root / "fs_map", args.ensure_workdirs)
    _generate_runs(eval_root, dst_root / "runs")
    print(f"[ok] generated docker runs: {dst_root / 'runs'}")
    print(f"[ok] generated docker fs_map: {dst_root / 'fs_map'}")


if __name__ == "__main__":
    main()
