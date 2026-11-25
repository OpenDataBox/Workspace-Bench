"""Validation shared by fresh and persisted agent-as-a-judge rubric verdicts."""
from __future__ import annotations

from typing import Any


def validated_rubric_rows(payload: Any, expected_count: int) -> list[dict[str, Any]] | None:
    """Return the rows when each expected index has exactly one Boolean verdict."""
    if not isinstance(payload, dict):
        return None
    rows = payload.get("rubrics")
    if not isinstance(rows, list) or len(rows) != expected_count:
        return None

    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            return None
        index = row.get("index")
        if type(index) is not int or not 0 <= index < expected_count or index in seen:
            return None
        if not isinstance(row.get("passed"), bool):
            return None
        seen.add(index)
    return rows
