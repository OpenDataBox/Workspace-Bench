#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail if an agent_runner report contains non-passed cases.")
    parser.add_argument("report", help="Path to agent_runner_report.json")
    args = parser.parse_args()

    report_path = Path(args.report)
    if not report_path.is_file():
        print(f"[error] missing report: {report_path}", file=sys.stderr)
        return 2

    data = json.loads(report_path.read_text(encoding="utf-8"))
    summary = data.get("summary") if isinstance(data, dict) else None
    if not isinstance(summary, dict):
        print(f"[error] invalid report summary: {report_path}", file=sys.stderr)
        return 2

    failed = int(summary.get("failed") or 0)
    errors = int(summary.get("error") or 0)
    timeouts = int(summary.get("timeout") or 0)
    total = int(summary.get("total") or 0)
    passed = int(summary.get("passed") or 0)

    if failed or errors or timeouts or passed != total:
        print(f"[error] smoke report has non-passed cases: {report_path}", file=sys.stderr)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1

    print(f"[ok] {report_path}: {passed}/{total} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
