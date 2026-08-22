#!/usr/bin/env python3
"""Build one auditable report from independently containerized task runs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml


Json = Any


def _read_json(path: Path) -> Json:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _read_yaml(path: Path) -> dict[str, Json]:
    with path.open("r", encoding="utf-8") as f:
        value = yaml.safe_load(f)
    if not isinstance(value, dict):
        raise ValueError("run config must be a mapping")
    return value


def _safe_name(value: str) -> str:
    return "".join(ch if (ch.isalnum() or ch in {"-", "_", "."}) else "_" for ch in str(value or ""))[:120] or "item"


def _case_from_agent(case_id: str, case_dir: Path, agent: dict[str, Json]) -> dict[str, Json]:
    trace = agent.get("trace") if isinstance(agent.get("trace"), dict) else {}
    outputs = trace.get("outputs") if isinstance(trace.get("outputs"), dict) else {}
    manifest = outputs.get("outputManifest") if isinstance(outputs.get("outputManifest"), list) else []
    return {
        "caseId": case_id,
        "outputDir": str(case_dir),
        "status": str(agent.get("status") or "error"),
        "durationMs": agent.get("durationMs") if isinstance(agent.get("durationMs"), int) else None,
        "outputFiles": [item for item in manifest if isinstance(item, dict)],
        "errorType": agent.get("errorType"),
        "errorMessage": agent.get("errorMessage"),
        "workDirRetained": agent.get("workDirRetained"),
    }


def _storage_quota_modes(runs_root: Path, task_ids: list[str]) -> list[str]:
    modes: set[str] = set()
    for task_id in task_ids:
        path = runs_root / _safe_name(task_id) / "raw" / "container-isolation.json"
        if not path.is_file():
            continue
        try:
            value = _read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and isinstance(value.get("storageQuotaMode"), str):
            modes.add(value["storageQuotaMode"])
    return sorted(modes)


def main() -> None:
    parser = argparse.ArgumentParser(description="Aggregate separately containerized task runs.")
    parser.add_argument("--run-config", required=True)
    parser.add_argument("--task-ids", nargs="+", required=True)
    args = parser.parse_args()

    config = _read_yaml(Path(args.run_config))
    output_dir = Path(str(config["output_dir"]))
    runs_root = output_dir / f"{config['agent_name']}--{config['model_name']}--{config['run_name']}"
    summary = {"total": 0, "passed": 0, "failed": 0, "error": 0, "timeout": 0}
    cases: list[dict[str, Json]] = []

    for task_id in args.task_ids:
        case_dir = runs_root / _safe_name(task_id)
        agent_path = case_dir / "agent.json"
        if agent_path.is_file():
            agent = _read_json(agent_path)
            if not isinstance(agent, dict):
                agent = {}
            case = _case_from_agent(task_id, case_dir, agent)
        else:
            isolation_path = case_dir / "raw" / "container-isolation.json"
            isolation = _read_json(isolation_path) if isolation_path.is_file() else {}
            reason = isolation.get("terminationReason") if isinstance(isolation, dict) else None
            case = {
                "caseId": task_id,
                "outputDir": str(case_dir),
                "status": "timeout" if reason == "wall_clock_limit" else "error",
                "durationMs": isolation.get("durationMs") if isinstance(isolation, dict) else None,
                "outputFiles": [],
                "errorType": "StorageLimit" if reason == "storage_limit" else ("Timeout" if reason == "wall_clock_limit" else "RunnerError"),
                "errorMessage": f"Task container terminated: {reason or 'runner did not produce agent.json'}",
                "workDirRetained": False,
            }
        status = str(case["status"])
        summary["total"] += 1
        summary[status if status in summary else "error"] += 1
        cases.append(case)

    report_config = json.loads(json.dumps(config, ensure_ascii=False, default=str))
    if isinstance(report_config.get("api_provider"), dict):
        report_config["api_provider"].pop("apiKey", None)
    report_config["task_isolation"] = "container"
    report_config["executed_task_ids"] = list(args.task_ids)
    report = {
        "runsRoot": str(runs_root),
        "agentId": config["agent_name"],
        "summary": summary,
        "cases": cases,
        "config": report_config,
        "isolation": {
            "mode": "per-task-container",
            "containerRemoval": "docker compose run --rm",
            "evidenceFile": "<case>/raw/container-isolation.json",
            "storageQuotaModes": _storage_quota_modes(runs_root, list(args.task_ids)),
        },
    }
    runs_root.mkdir(parents=True, exist_ok=True)
    (runs_root / "agent_runner_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
