#!/usr/bin/env python3
import argparse
import os
import re
import runpy
from pathlib import Path
from typing import Any

import yaml


Json = Any


def _expand_env_string(value: str) -> str:
    fallback_re = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)[:-]-(\$\{[A-Za-z_][A-Za-z0-9_]*\}|[^}]*)\}")
    s = value
    while True:
        m = fallback_re.search(s)
        if not m:
            break
        primary = os.environ.get(m.group(1), "")
        fallback = m.group(2)
        repl = primary if primary else os.path.expandvars(fallback)
        s = s[: m.start()] + repl + s[m.end() :]
    return os.path.expandvars(s)


def _expand_config_env(value: Json) -> Json:
    if isinstance(value, str):
        return _expand_env_string(value)
    if isinstance(value, list):
        return [_expand_config_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_config_env(v) for k, v in value.items()}
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare workdirs for a run config.")
    parser.add_argument("--run-config", required=True)
    args = parser.parse_args()

    eval_root = Path(__file__).resolve().parents[1]
    run_config_path = Path(args.run_config)
    if not run_config_path.is_absolute():
        run_config_path = (eval_root / run_config_path).resolve()

    cfg = yaml.safe_load(run_config_path.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise SystemExit(f"run config must be a mapping: {run_config_path}")
    cfg = _expand_config_env(cfg)

    task_path = str(cfg.get("task_path") or "").strip()
    fs_map_file = str(cfg.get("fs_map_file") or "").strip()
    if not task_path or not fs_map_file:
        raise SystemExit("run config must include task_path and fs_map_file")

    os.environ["TASK_DIR"] = task_path
    os.environ["FS_MAP_FILE"] = fs_map_file
    runpy.run_path(str(eval_root / "src" / "filesys_utils.py"), run_name="__main__")


if __name__ == "__main__":
    main()
