#!/usr/bin/env python3
"""Explicitly apply optional repository fixes to a downloaded task set.

The downloader intentionally does not call this script automatically so the
published Hugging Face snapshot remains comparable with leaderboard runs.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


EVAL_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = EVAL_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from task_patches import apply_task_patches


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("lite", "full"), default="lite")
    parser.add_argument("--language", choices=("en", "cn"), default="cn")
    parser.add_argument("--eval-root", type=Path, default=EVAL_ROOT)
    args = parser.parse_args()

    task_dir = args.eval_root.resolve() / ("tasks_lite" if args.kind == "lite" else "tasks")
    if not task_dir.is_dir():
        raise SystemExit(f"task directory not found: {task_dir}")
    patched = apply_task_patches(task_dir, kind=args.kind, language=args.language)
    print(f"[ok] applied {len(patched)} task patches: {', '.join(patched) if patched else 'none'}")
    if patched:
        print("[note] patched only the local task copy; Hugging Face assets were not modified")


if __name__ == "__main__":
    main()
