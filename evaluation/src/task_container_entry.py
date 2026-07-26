#!/usr/bin/env python3
"""Run one Workspace-Bench task under a bounded, disposable task container.

This program is the PID-1 workload of the ``workspace-bench-task`` Compose
service.  The service itself supplies the kernel-enforced CPU, memory, PID and
container-layer limits.  This wrapper adds two guarantees that Docker's
resource controls do not cover for bind mounts:

* it starts the normal runner in a new process group and kills that complete
  group on a wall-clock or case-directory storage violation; and
* it writes an auditable isolation record into the task result directory.

The host-side ``run_isolated_benchmark.py`` launches a fresh service container
for every task with ``docker compose run --rm``.  Consequently every process,
temporary directory, HOME directory, and in-memory harness state disappears
when the task container exits.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml


Json = Any
DEFAULT_GRACE_SECONDS = 30.0
POLL_SECONDS = 0.25


def _safe_name(value: str) -> str:
    return "".join(ch if (ch.isalnum() or ch in {"-", "_", "."}) else "_" for ch in str(value or ""))[:120] or "item"


def _read_config(path: str) -> dict[str, Json]:
    with open(path, "r", encoding="utf-8") as f:
        value = yaml.safe_load(f)
    if not isinstance(value, dict):
        raise ValueError("run config must be a mapping")
    return value


def _runs_root(config: dict[str, Json]) -> Path:
    output_dir = str(config.get("output_dir") or "").strip()
    agent_name = str(config.get("agent_name") or "").strip()
    model_name = str(config.get("model_name") or "").strip()
    run_name = str(config.get("run_name") or "").strip()
    if not (output_dir and agent_name and model_name and run_name):
        raise ValueError("run config is missing output_dir, agent_name, model_name, or run_name")
    return Path(output_dir) / f"{agent_name}--{model_name}--{run_name}"


def _directory_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return total
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in files:
            candidate = Path(root) / name
            try:
                if candidate.is_symlink():
                    continue
                total += candidate.stat(follow_symlinks=False).st_size
            except OSError:
                continue
        # Do not descend into symlinked directories even on platforms where
        # os.walk's default behaviour changes.
        dirs[:] = [name for name in dirs if not (Path(root) / name).is_symlink()]
    return total


def _terminate_group(proc: subprocess.Popen[bytes], *, grace_seconds: float) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        try:
            proc.terminate()
        except OSError:
            return

    try:
        proc.wait(timeout=max(1.0, grace_seconds))
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        try:
            proc.kill()
        except OSError:
            return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _resource_profile(config: dict[str, Json]) -> dict[str, Json]:
    configured = config.get("task_resources") if isinstance(config.get("task_resources"), dict) else {}
    return {
        "cpus": str(os.environ.get("WORKSPACE_BENCH_TASK_CPUS") or configured.get("cpus") or "2"),
        "memoryMb": int(os.environ.get("WORKSPACE_BENCH_TASK_MEMORY_MB") or configured.get("memory_mb") or 8192),
        "pids": int(os.environ.get("WORKSPACE_BENCH_TASK_PIDS") or configured.get("pids") or 512),
        "storageMb": int(os.environ.get("WORKSPACE_BENCH_TASK_STORAGE_MB") or configured.get("storage_mb") or 20480),
    }


def _write_record(path: Path, value: dict[str, Json]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one task with process-tree and storage enforcement.")
    parser.add_argument("--run-config", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--grace-seconds", type=float, default=DEFAULT_GRACE_SECONDS)
    args = parser.parse_args()

    config = _read_config(args.run_config)
    timeout_seconds = float(config.get("timeout_sec") or 300.0)
    resources = _resource_profile(config)
    storage_bytes = int(resources["storageMb"]) * 1024 * 1024
    case_dir = _runs_root(config) / _safe_name(args.task_id)
    record_path = case_dir / "raw" / "container-isolation.json"
    # A result directory from a prior invocation would make agent_runner resume
    # rather than execute the task.  Remove only this exact case directory so
    # every isolated invocation has a new workspace and a new process tree.
    if case_dir.exists():
        shutil.rmtree(case_dir)
    started = time.time()
    reason: str | None = None

    runner_path = Path(__file__).with_name("agent_runner.py")
    cmd = [sys.executable, "-u", str(runner_path), "--run-config", args.run_config]
    proc = subprocess.Popen(cmd, start_new_session=True)
    max_duration = max(1.0, timeout_seconds) + max(1.0, args.grace_seconds)

    while proc.poll() is None:
        elapsed = time.time() - started
        if elapsed > max_duration:
            reason = "wall_clock_limit"
            _terminate_group(proc, grace_seconds=args.grace_seconds)
            break
        if _directory_size(case_dir) > storage_bytes:
            reason = "storage_limit"
            _terminate_group(proc, grace_seconds=args.grace_seconds)
            break
        time.sleep(POLL_SECONDS)

    if proc.poll() is None:
        _terminate_group(proc, grace_seconds=args.grace_seconds)
    return_code = int(proc.returncode if proc.returncode is not None else 1)
    finished = time.time()
    record: dict[str, Json] = {
        "schemaVersion": 1,
        "mode": "per-task-container",
        "containerName": os.environ.get("WORKSPACE_BENCH_TASK_CONTAINER_NAME"),
        "containerId": os.environ.get("HOSTNAME"),
        "imageDigest": os.environ.get("WORKSPACE_BENCH_TASK_IMAGE_DIGEST"),
        "taskId": str(args.task_id),
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "finishedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(finished)),
        "durationMs": int((finished - started) * 1000),
        "timeoutSeconds": timeout_seconds,
        "resourceProfile": resources,
        "storageBytesObserved": _directory_size(case_dir),
        "terminationReason": reason,
        "processGroupTerminated": reason is not None,
        "containerRemovalRequested": True,
        "exitCode": return_code,
    }
    _write_record(record_path, record)

    if reason == "wall_clock_limit":
        return 124
    if reason == "storage_limit":
        return 122
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
