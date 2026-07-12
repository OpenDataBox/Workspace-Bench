#!/usr/bin/env python3
"""Rescore existing judge outputs with a preliminary rubric-selection audit.

This script does not rerun agents or judges. It joins each
``rubrics_judge--*.json`` row with a CSV audit by ``(task_id, rubric_index)``
and reports both the observed original score and a selected-rubric score.

The default selection is ``basis_label == 有依据`` from the repository's
Chinese Lite audit. That label means input-grounded only; it is not a final
fairness or equivalence certification.
"""

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


Json = Any
SCRIPT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AUDIT_CSV = SCRIPT_ROOT / "evaluation/audits/workspace_bench_lite_cn_rubric_grounding_audit_v0.1.csv"
SUMMARY_FILE_NAME = "preliminary_grounded_summary.json"
SCHEMA_VERSION = "workspace-bench.preliminary-grounded.v0.1"


def _read_json(path: Path) -> Json:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _rate(passed: int, total: int) -> float | None:
    return passed / total if total else None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _format_rate(value: float | None) -> str:
    return f"{value:.2%}" if value is not None else "n/a"


def _csv_task_id(row: dict[str, str]) -> str:
    return str(row.get("task_id") or "").strip()


def load_audit(
    audit_csv: Path,
    include_label: str,
) -> tuple[set[tuple[str, int]], set[tuple[str, int]], Counter[str], int]:
    selected: set[tuple[str, int]] = set()
    all_rubrics: set[tuple[str, int]] = set()
    labels: Counter[str] = Counter()
    rows = 0

    with audit_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"task_id", "rubric_index", "basis_label"}
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(f"audit CSV is missing columns: {', '.join(sorted(missing))}")

        for row_number, row in enumerate(reader, start=2):
            task_id = _csv_task_id(row)
            try:
                rubric_index = int(str(row.get("rubric_index") or "").strip())
            except ValueError as error:
                raise ValueError(f"invalid rubric_index at audit CSV row {row_number}") from error
            if not task_id or rubric_index < 0:
                raise ValueError(f"invalid task_id or rubric_index at audit CSV row {row_number}")

            key = (task_id, rubric_index)
            if key in all_rubrics:
                raise ValueError(f"duplicate audit rubric: {task_id}:{rubric_index}")

            label = str(row.get("basis_label") or "").strip()
            all_rubrics.add(key)
            labels[label] += 1
            rows += 1
            if label == include_label:
                selected.add(key)

    return selected, all_rubrics, labels, rows


def _judge_rows(path: Path) -> tuple[str, list[dict[str, Json]], list[str]]:
    data = _read_json(path)
    if not isinstance(data, dict):
        raise ValueError("judge output must be a JSON object")

    task_id = str(data.get("taskId") or path.parent.name).strip()
    if not task_id:
        raise ValueError("missing taskId")

    rubrics = data.get("rubrics")
    if not isinstance(rubrics, list):
        raise ValueError("missing rubrics list")

    rows: list[dict[str, Json]] = []
    warnings: list[str] = []
    seen_indices: set[int] = set()
    for position, row in enumerate(rubrics):
        if not isinstance(row, dict):
            warnings.append(f"non-object rubric row at position {position}")
            continue
        index = row.get("index")
        if not isinstance(index, int) or index < 0:
            warnings.append(f"invalid rubric index at position {position}")
            continue
        if index in seen_indices:
            warnings.append(f"duplicate rubric index {index}")
            continue
        seen_indices.add(index)
        passed = row.get("passed")
        if not isinstance(passed, bool):
            warnings.append(f"non-boolean passed value for rubric index {index}; counted as failed")
            passed = False
        rows.append({"index": index, "passed": passed})

    return task_id, rows, warnings


def _summarize_unit(
    *,
    task_id: str,
    result_file: Path,
    results_root: Path,
    rows: list[dict[str, Json]],
    selected: set[tuple[str, int]],
    audit_rubrics: set[tuple[str, int]],
) -> dict[str, Json]:
    row_by_index = {int(row["index"]): bool(row["passed"]) for row in rows}
    expected = {index for audit_task_id, index in selected if audit_task_id == task_id}
    audited = {index for audit_task_id, index in audit_rubrics if audit_task_id == task_id}
    included = {index: passed for index, passed in row_by_index.items() if index in expected}
    missing = sorted(expected.difference(row_by_index))
    untracked = sorted(index for index in row_by_index if index not in audited)

    original_passed = sum(row_by_index.values())
    grounded_passed = sum(included.values())
    return {
        "taskId": task_id,
        "resultFile": str(result_file.relative_to(results_root)),
        "original": {
            "passed": original_passed,
            "judged": len(row_by_index),
            "passRate": _rate(original_passed, len(row_by_index)),
        },
        "preliminaryGrounded": {
            "passed": grounded_passed,
            "judged": len(included),
            "eligible": len(expected),
            "missing": len(missing),
            "missingIndices": missing,
            "passRate": _rate(grounded_passed, len(included)),
            "coverage": _rate(len(included), len(expected)),
        },
        "untrackedJudgeIndices": untracked,
    }


def rescore(results_root: Path, audit_csv: Path, include_label: str) -> dict[str, Json]:
    selected, audit_rubrics, label_counts, audit_rows = load_audit(audit_csv, include_label)
    judge_files = sorted(results_root.rglob("rubrics_judge--*.json"))
    if not judge_files:
        raise ValueError(f"no rubrics_judge--*.json files found under {results_root}")

    units: list[dict[str, Json]] = []
    warnings: list[str] = []
    for judge_file in judge_files:
        try:
            task_id, rows, row_warnings = _judge_rows(judge_file)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            warnings.append(f"{judge_file.relative_to(results_root)}: skipped ({error})")
            continue
        units.append(
            _summarize_unit(
                task_id=task_id,
                result_file=judge_file,
                results_root=results_root,
                rows=rows,
                selected=selected,
                audit_rubrics=audit_rubrics,
            )
        )
        warnings.extend(f"{judge_file.relative_to(results_root)}: {warning}" for warning in row_warnings)

    if not units:
        raise ValueError("no readable judge outputs found")

    original_passed = sum(int(unit["original"]["passed"]) for unit in units)
    original_judged = sum(int(unit["original"]["judged"]) for unit in units)
    grounded_passed = sum(int(unit["preliminaryGrounded"]["passed"]) for unit in units)
    grounded_judged = sum(int(unit["preliminaryGrounded"]["judged"]) for unit in units)
    grounded_eligible = sum(int(unit["preliminaryGrounded"]["eligible"]) for unit in units)
    grounded_missing = sum(int(unit["preliminaryGrounded"]["missing"]) for unit in units)
    original_task_rates = [
        float(rate) for unit in units if (rate := unit["original"]["passRate"]) is not None
    ]
    grounded_task_rates = [
        float(rate) for unit in units if (rate := unit["preliminaryGrounded"]["passRate"]) is not None
    ]

    repeated_tasks = Counter(str(unit["taskId"]) for unit in units)
    for task_id, count in sorted(repeated_tasks.items()):
        if count > 1:
            warnings.append(
                f"task {task_id} has {count} judge files; each file is counted as a separate result unit"
            )
    for unit in units:
        missing = int(unit["preliminaryGrounded"]["missing"])
        if missing:
            warnings.append(
                f"{unit['resultFile']}: {missing} selected rubric(s) missing from judge output"
            )
        untracked = unit["untrackedJudgeIndices"]
        if untracked:
            warnings.append(f"{unit['resultFile']}: judge indices absent from audit: {untracked}")

    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "selection": {
            "auditCsv": str(audit_csv.resolve()),
            "auditSha256": hashlib.sha256(audit_csv.read_bytes()).hexdigest(),
            "includeLabel": include_label,
            "auditRows": audit_rows,
            "selectedRubrics": len(selected),
            "labelCounts": dict(sorted(label_counts.items())),
            "knownLimit": (
                "The selection reflects input grounding only. It does not certify task alignment, "
                "uniqueness, or acceptance of semantically equivalent solutions."
            ),
        },
        "input": {
            "resultsRoot": str(results_root.resolve()),
            "judgeFilesFound": len(judge_files),
            "judgeFilesScored": len(units),
            "resultUnits": len(units),
            "distinctTasks": len(repeated_tasks),
        },
        "metrics": {
            "originalObserved": {
                "passed": original_passed,
                "judged": original_judged,
                "microPassRate": _rate(original_passed, original_judged),
                "resultUnitMacroPassRate": _mean(original_task_rates),
            },
            "preliminaryGrounded": {
                "passed": grounded_passed,
                "judged": grounded_judged,
                "eligible": grounded_eligible,
                "missing": grounded_missing,
                "microPassRate": _rate(grounded_passed, grounded_judged),
                "resultUnitMacroPassRate": _mean(grounded_task_rates),
                "coverage": _rate(grounded_judged, grounded_eligible),
            },
        },
        "resultUnits": units,
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rescore existing rubric judge outputs with a preliminary audit selection."
    )
    parser.add_argument("results_root", type=Path, help="Run directory containing rubrics_judge--*.json files")
    parser.add_argument(
        "--audit-csv",
        type=Path,
        default=DEFAULT_AUDIT_CSV,
        help=f"Audit CSV (default: {DEFAULT_AUDIT_CSV})",
    )
    parser.add_argument(
        "--include-label",
        default="有依据",
        help="Audit basis_label to include (default: 有依据)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=f"Summary JSON path (default: <results_root>/{SUMMARY_FILE_NAME})",
    )
    args = parser.parse_args(argv)

    results_root = args.results_root.resolve()
    audit_csv = args.audit_csv.resolve()
    if not results_root.is_dir():
        parser.error(f"results_root is not a directory: {results_root}")
    if not audit_csv.is_file():
        parser.error(f"audit CSV does not exist: {audit_csv}")

    try:
        summary = rescore(results_root, audit_csv, args.include_label)
    except ValueError as error:
        parser.error(str(error))

    output_path = args.output or results_root / SUMMARY_FILE_NAME
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    metrics = summary["metrics"]
    original = metrics["originalObserved"]
    grounded = metrics["preliminaryGrounded"]
    print(
        "original observed: "
        f"{original['passed']}/{original['judged']} ({_format_rate(original['microPassRate'])})"
    )
    print(
        "preliminary grounded: "
        f"{grounded['passed']}/{grounded['judged']} ({_format_rate(grounded['microPassRate'])}); "
        f"coverage {grounded['judged']}/{grounded['eligible']} ({_format_rate(grounded['coverage'])})"
    )
    print(f"summary: {output_path}")
    if summary["warnings"]:
        print(f"warnings: {len(summary['warnings'])} (see summary JSON)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
