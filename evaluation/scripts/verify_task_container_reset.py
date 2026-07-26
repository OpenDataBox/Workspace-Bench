#!/usr/bin/env python3
"""Integration check for the disposable task-container contract.

The first task container creates markers in its task-local temporary and home
directories and starts a background process.  It is removed with ``--rm``.
The second fresh task container verifies that neither marker is present.  The
script also asks the Docker daemon to confirm that both named containers no
longer exist after their runs.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=str(cwd), text=True, capture_output=True, check=False)


def _compose(compose_file: Path, name: str, command: str) -> list[str]:
    return [
        "docker",
        "compose",
        "-f",
        str(compose_file),
        "run",
        "--rm",
        "--no-deps",
        "--name",
        name,
        "workspace-bench-task",
        "bash",
        "-lc",
        command,
    ]


def _removed(name: str, *, cwd: Path) -> bool:
    result = _run(["docker", "container", "inspect", name], cwd=cwd)
    return result.returncode != 0


def main() -> int:
    eval_root = Path(__file__).resolve().parents[1]
    compose_file = eval_root / "docker" / "docker-compose.yaml"
    token = f"workspace-bench-reset-{uuid.uuid4().hex}"
    first = f"workspace-bench-reset-a-{uuid.uuid4().hex[:10]}"
    second = f"workspace-bench-reset-b-{uuid.uuid4().hex[:10]}"

    write = _run(
        _compose(
            compose_file,
            first,
            # Close the child process's inherited stdio so Docker does not
            # wait for it to drain the command's log pipes.  Container removal
            # then kills this deliberately orphaned background process.
            f"touch /tmp/{token} \"$HOME/{token}\"; (sleep 120 </dev/null >/dev/null 2>&1 &) ; true",
        ),
        cwd=eval_root,
    )
    if write.returncode != 0 or not _removed(first, cwd=eval_root):
        sys.stderr.write(write.stderr or write.stdout or "first task container did not terminate cleanly\n")
        return 1

    verify = _run(
        _compose(
            compose_file,
            second,
            f"test ! -e /tmp/{token} && test ! -e \"$HOME/{token}\"",
        ),
        cwd=eval_root,
    )
    if verify.returncode != 0 or not _removed(second, cwd=eval_root):
        sys.stderr.write(verify.stderr or verify.stdout or "fresh task container observed residual state\n")
        return 1

    print("[ok] task containers are removed and task-local HOME/tmp state is reset")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
