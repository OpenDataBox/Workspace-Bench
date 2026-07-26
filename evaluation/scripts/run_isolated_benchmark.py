#!/usr/bin/env python3
"""Host-side launcher for the reproducible per-task-container protocol.

The regular runner remains useful for local development.  This launcher is the
recommended evaluation path: it first materializes one pristine standard
workspace, then evaluates each selected task in a newly created and removed
``workspace-bench-task`` container.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any


Json = Any
DEFAULT_RESOURCES = {"cpus": "2", "memory_mb": 8192, "pids": 512, "storage_mb": 20480}


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value)).strip("-")[:48] or "task"


def _value_after(args: list[str], flag: str, default: str | None = None) -> str | None:
    for index, value in enumerate(args):
        if value == flag and index + 1 < len(args):
            return args[index + 1]
    return default


def _split_benchmark_args(args: list[str]) -> tuple[list[str], list[str], str | None, str]:
    """Remove selection and run-name flags, retaining all other runner flags."""
    base: list[str] = []
    requested_ids: list[str] = []
    persona: str | None = None
    run_name: str | None = None
    selection_flags = 0
    index = 0
    while index < len(args):
        value = args[index]
        if value == "--task-ids":
            selection_flags += 1
            index += 1
            count_before = len(requested_ids)
            while index < len(args) and not args[index].startswith("--"):
                requested_ids.extend(part for part in args[index].split(",") if part)
                index += 1
            if len(requested_ids) == count_before:
                raise SystemExit("--task-ids requires at least one task id")
            continue
        if value in {"--task-limit", "--persona", "--run-name", "--task-parallel-workers"}:
            if index + 1 >= len(args):
                raise SystemExit(f"{value} requires a value")
            option_value = args[index + 1]
            if value == "--task-limit":
                selection_flags += 1
                requested_ids = [f"__limit__:{option_value}"]
            elif value == "--persona":
                selection_flags += 1
                persona = option_value
            elif value == "--run-name":
                run_name = option_value
            index += 2
            continue
        if value == "--no-task-parallel":
            index += 1
            continue
        base.append(value)
        index += 1

    dataset = str(_value_after(base, "--dataset", "lite") or "lite").strip().lower()
    if dataset not in {"smoke", "lite", "full"}:
        raise SystemExit(f"unsupported dataset: {dataset}")
    if selection_flags > 1:
        raise SystemExit("--task-limit, --task-ids, and --persona are mutually exclusive")
    return base, requested_ids, persona, run_name or {"smoke": "Smoke", "lite": "Lite", "full": "Full"}[dataset]


def _selected_task_ids(eval_root: Path, *, dataset: str, requested: list[str], persona: str | None) -> list[str]:
    task_root = eval_root / ("tasks" if dataset == "full" else "tasks_lite")
    if not task_root.is_dir():
        raise SystemExit(f"task directory not found: {task_root}; download the selected dataset first")
    metadata: list[dict[str, Json]] = []
    for path in sorted(task_root.glob("*/metadata.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SystemExit(f"invalid metadata: {path}: {exc}") from exc
        if isinstance(value, dict) and str(value.get("id") or "").strip():
            metadata.append(value)

    by_id = {str(item["id"]): item for item in metadata}
    if requested:
        if len(requested) == 1 and requested[0].startswith("__limit__:"):
            try:
                limit = max(0, int(requested[0].split(":", 1)[1]))
            except ValueError as exc:
                raise SystemExit("--task-limit must be an integer") from exc
            return [str(item["id"]) for item in metadata[:limit]]
        duplicates = sorted({item for item in requested if requested.count(item) > 1})
        missing = [item for item in requested if item not in by_id]
        if duplicates or missing:
            problem = []
            if duplicates:
                problem.append("duplicate task id(s): " + ", ".join(duplicates))
            if missing:
                problem.append("unknown task id(s): " + ", ".join(missing))
            raise SystemExit("; ".join(problem))
        return requested
    if persona is not None:
        selected = [str(item["id"]) for item in metadata if str(item.get("persona") or "") == persona]
        if not selected:
            raise SystemExit(f"no tasks for persona: {persona}")
        return selected
    return [str(item["id"]) for item in (metadata[:1] if dataset == "smoke" else metadata)]


def _compose_command(
    compose_file: Path,
    service: str,
    command: list[str],
    *,
    container_name: str | None = None,
    service_env: dict[str, str] | None = None,
) -> list[str]:
    out = ["docker", "compose", "-f", str(compose_file), "run", "--rm", "--no-deps"]
    if container_name:
        out.extend(["--name", container_name])
    for key, value in sorted((service_env or {}).items()):
        out.extend(["-e", f"{key}={value}"])
    out.append(service)
    out.extend(command)
    return out


def _run(command: list[str], *, cwd: Path, env: dict[str, str], capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=str(cwd), env=env, text=True, check=False, capture_output=capture)


def _build_config(
    *, compose_file: Path, eval_root: Path, env: dict[str, str], base_args: list[str], task_id: str, run_name: str, resources: dict[str, Json]
) -> str:
    command = _compose_command(
        compose_file,
        "workspace-bench",
        [
            "python3",
            "/workspace/Workspace-Bench/evaluation/scripts/build_run_config.py",
            "--eval-root",
            "/workspace/Workspace-Bench/evaluation",
            *base_args,
            "--task-ids",
            task_id,
            "--run-name",
            run_name,
            "--no-task-parallel",
            "--task-isolation",
            "container",
            "--task-cpus",
            str(resources["cpus"]),
            "--task-memory-mb",
            str(resources["memory_mb"]),
            "--task-pids",
            str(resources["pids"]),
            "--task-storage-mb",
            str(resources["storage_mb"]),
        ],
    )
    result = _run(command, cwd=eval_root, env=env, capture=True)
    if result.returncode != 0:
        raise SystemExit(result.stderr or result.stdout or "failed to build isolated task config")
    paths = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not paths:
        raise SystemExit("build_run_config.py did not return a config path")
    return paths[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate each selected task in a fresh constrained Docker container.")
    parser.add_argument("--task-cpus", default=DEFAULT_RESOURCES["cpus"])
    parser.add_argument("--task-memory-mb", type=int, default=DEFAULT_RESOURCES["memory_mb"])
    parser.add_argument("--task-pids", type=int, default=DEFAULT_RESOURCES["pids"])
    parser.add_argument("--task-storage-mb", type=int, default=DEFAULT_RESOURCES["storage_mb"])
    # The benchmark runner has its own CLI.  Keep its flags opaque here so the
    # recommended command can remain natural (no mandatory `--` separator).
    args, raw_args = parser.parse_known_args()
    raw_args = list(raw_args)
    if raw_args[:1] == ["--"]:
        raw_args = raw_args[1:]
    if not raw_args:
        raw_args = ["--harness", "codex", "--model", "kimi-k2.5", "--dataset", "lite"]
    if not _value_after(raw_args, "--harness") or not _value_after(raw_args, "--model"):
        raise SystemExit("benchmark arguments must include --harness and --model")

    base_args, requested_ids, persona, run_name = _split_benchmark_args(raw_args)
    dataset = str(_value_after(base_args, "--dataset", "lite") or "lite").lower()
    eval_root = Path(__file__).resolve().parents[1]
    compose_file = eval_root / "docker" / "docker-compose.yaml"
    task_ids = _selected_task_ids(eval_root, dataset=dataset, requested=requested_ids, persona=persona)
    if not task_ids:
        raise SystemExit("task selection is empty")
    resources: dict[str, Json] = {
        "cpus": str(args.task_cpus),
        "memory_mb": max(1, int(args.task_memory_mb)),
        "pids": max(1, int(args.task_pids)),
        "storage_mb": max(1, int(args.task_storage_mb)),
    }
    env = dict(os.environ)
    env.update(
        {
            "WORKSPACE_BENCH_TASK_CPUS": str(resources["cpus"]),
            "WORKSPACE_BENCH_TASK_MEMORY": f"{resources['memory_mb']}m",
            "WORKSPACE_BENCH_TASK_PIDS": str(resources["pids"]),
            "WORKSPACE_BENCH_TASK_STORAGE": f"{resources['storage_mb']}m",
            "WORKSPACE_BENCH_TASK_STORAGE_MB": str(resources["storage_mb"]),
            "WORKSPACE_BENCH_TASK_TMPFS": f"{resources['storage_mb']}m",
        }
    )

    configs = [
        _build_config(
            compose_file=compose_file,
            eval_root=eval_root,
            env=env,
            base_args=base_args,
            task_id=task_id,
            run_name=run_name,
            resources=resources,
        )
        for task_id in task_ids
    ]
    prepare = _compose_command(
        compose_file,
        "workspace-bench",
        ["python3", "/workspace/Workspace-Bench/evaluation/scripts/prepare_workdirs_for_run.py", "--run-config", configs[0]],
    )
    prepared = _run(prepare, cwd=eval_root, env=env)
    if prepared.returncode != 0:
        return prepared.returncode

    image = _run(
        ["docker", "image", "inspect", "--format={{.Id}}", "workspace-bench:local"],
        cwd=eval_root,
        env=env,
        capture=True,
    )
    image_digest = image.stdout.strip() if image.returncode == 0 else ""

    failures = 0
    for task_id, config_path in zip(task_ids, configs):
        name = f"workspace-bench-task-{_safe_name(task_id)}-{uuid.uuid4().hex[:8]}"
        task_env = dict(env)
        task_env["WORKSPACE_BENCH_TASK_CONTAINER_NAME"] = name
        command = _compose_command(
            compose_file,
            "workspace-bench-task",
            [
                "python3",
                "-u",
                "/workspace/Workspace-Bench/evaluation/src/task_container_entry.py",
                "--run-config",
                config_path,
                "--task-id",
                task_id,
            ],
            container_name=name,
            service_env={
                "WORKSPACE_BENCH_TASK_CONTAINER_NAME": name,
                "WORKSPACE_BENCH_TASK_IMAGE_DIGEST": image_digest,
            },
        )
        result = _run(command, cwd=eval_root, env=task_env)
        if result.returncode != 0:
            failures += 1

    aggregate = _compose_command(
        compose_file,
        "workspace-bench",
        [
            "python3",
            "/workspace/Workspace-Bench/evaluation/scripts/aggregate_isolated_run.py",
            "--run-config",
            configs[0],
            "--task-ids",
            *task_ids,
        ],
    )
    aggregate_result = _run(aggregate, cwd=eval_root, env=env)
    return aggregate_result.returncode if aggregate_result.returncode != 0 else (1 if failures else 0)


if __name__ == "__main__":
    raise SystemExit(main())
