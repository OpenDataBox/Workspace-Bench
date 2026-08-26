#!/usr/bin/env python3
"""Apply repository-maintained corrections to downloaded benchmark tasks."""

from __future__ import annotations

import json
import math
import zipfile
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape


DEFAULT_PATCH_ROOT = Path(__file__).resolve().parents[1] / "task_patches"


def _merge_dict(base: dict[str, Any], update: dict[str, Any]) -> None:
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge_dict(base[key], value)
        else:
            base[key] = value


def _column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _cell_xml(row_index: int, column_index: int, value: Any) -> str:
    reference = f"{_column_name(column_index)}{row_index}"
    if value is None:
        return ""
    if isinstance(value, bool):
        return f'<c r="{reference}" t="b"><v>{int(value)}</v></c>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"non-finite spreadsheet value at {reference}")
        return f'<c r="{reference}"><v>{value}</v></c>'
    text = escape(str(value))
    return f'<c r="{reference}" t="inlineStr"><is><t>{text}</t></is></c>'


def _write_xlsx(path: Path, sheets: list[dict[str, Any]]) -> None:
    if not sheets:
        raise ValueError(f"generated xlsx has no sheets: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    content_types = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>',
    ]
    workbook_sheets = []
    workbook_rels = []
    worksheet_xml: list[tuple[str, str]] = []
    for index, sheet in enumerate(sheets, 1):
        name = str(sheet["name"])
        rows = sheet.get("rows")
        if not isinstance(rows, list):
            raise ValueError(f"generated xlsx sheet rows must be a list: {path}: {name}")
        content_types.append(
            f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
        workbook_sheets.append(
            f'<sheet name="{escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
        )
        workbook_rels.append(
            f'<Relationship Id="rId{index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
        )
        row_xml = []
        for row_index, row in enumerate(rows, 1):
            if not isinstance(row, list):
                raise ValueError(f"generated xlsx row must be a list: {path}: {name}:{row_index}")
            cells = "".join(
                _cell_xml(row_index, column_index, value)
                for column_index, value in enumerate(row, 1)
            )
            row_xml.append(f'<row r="{row_index}">{cells}</row>')
        worksheet_xml.append(
            (
                f"xl/worksheets/sheet{index}.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f'<sheetData>{"".join(row_xml)}</sheetData></worksheet>',
            )
        )
    content_types.append("</Types>")
    workbook_rels.append(
        f'<Relationship Id="rId{len(sheets) + 1}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
    )

    files = {
        "[Content_Types].xml": "".join(content_types),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/></Relationships>'
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets>{"".join(workbook_sheets)}</sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'{"".join(workbook_rels)}</Relationships>'
        ),
        "xl/styles.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
            '<borders count="1"><border/></borders>'
            '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
            '<cellXfs count="1"><xf xfId="0"/></cellXfs>'
            '</styleSheet>'
        ),
    }
    files.update(worksheet_xml)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)


def _copy_patch_file(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"task patch file not found: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())


def _validate_task(task_dir: Path, metadata: dict[str, Any]) -> None:
    rubrics = metadata.get("rubrics")
    rubric_types = metadata.get("rubric_types")
    if isinstance(rubrics, list) and isinstance(rubric_types, list) and len(rubrics) != len(rubric_types):
        raise ValueError(
            f"rubrics/rubric_types length mismatch for task {task_dir.name}: "
            f"{len(rubrics)} != {len(rubric_types)}"
        )
    manifest = metadata.get("data_manifest")
    for item in manifest if isinstance(manifest, list) else []:
        if not isinstance(item, dict):
            continue
        stored_relpath = item.get("stored_relpath")
        if not isinstance(stored_relpath, str) or not stored_relpath.strip():
            raise ValueError(f"invalid stored_relpath for task {task_dir.name}: {item!r}")
        source = (task_dir / stored_relpath).resolve()
        source.relative_to(task_dir.resolve())
        if not source.is_file():
            raise FileNotFoundError(f"patched task input not found: {source}")


def apply_task_patches(
    task_root: Path,
    *,
    kind: str,
    language: str,
    patch_root: Path | None = None,
) -> list[str]:
    """Apply metadata and generated-file patches, returning patched task IDs."""

    bundle = (patch_root or DEFAULT_PATCH_ROOT) / f"{kind}_{language}"
    if not bundle.is_dir():
        return []

    patched: list[str] = []
    def patch_sort_key(path: Path) -> tuple[int, int | str]:
        task_id = path.parent.name
        return (0, int(task_id)) if task_id.isdigit() else (1, task_id)

    for patch_path in sorted(bundle.glob("*/patch.json"), key=patch_sort_key):
        task_id = patch_path.parent.name
        task_dir = task_root / task_id
        metadata_path = task_dir / "metadata.json"
        if not metadata_path.is_file():
            continue
        patch = json.loads(patch_path.read_text(encoding="utf-8"))
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(patch, dict) or not isinstance(metadata, dict):
            raise ValueError(f"invalid task patch or metadata for task {task_id}")

        metadata_patch = patch.get("metadata", {})
        if not isinstance(metadata_patch, dict):
            raise ValueError(f"metadata patch must be an object for task {task_id}")
        _merge_dict(metadata, metadata_patch)

        generated_files = patch.get("generated_files", [])
        if not isinstance(generated_files, list):
            raise ValueError(f"generated_files must be a list for task {task_id}")
        for generated in generated_files:
            if not isinstance(generated, dict):
                raise ValueError(f"invalid generated file entry for task {task_id}")
            relpath = generated.get("stored_relpath")
            if not isinstance(relpath, str) or not relpath.strip():
                raise ValueError(f"missing generated stored_relpath for task {task_id}")
            destination = (task_dir / relpath).resolve()
            destination.relative_to(task_dir.resolve())
            if generated.get("type") != "xlsx":
                raise ValueError(f"unsupported generated file type for task {task_id}: {generated.get('type')}")
            sheets = generated.get("sheets")
            if not isinstance(sheets, list):
                raise ValueError(f"generated xlsx sheets must be a list for task {task_id}")
            _write_xlsx(destination, sheets)

        copied_files = patch.get("copy_files", [])
        if not isinstance(copied_files, list):
            raise ValueError(f"copy_files must be a list for task {task_id}")
        for copied in copied_files:
            if not isinstance(copied, dict):
                raise ValueError(f"invalid copy file entry for task {task_id}")
            source_relpath = copied.get("source")
            stored_relpath = copied.get("stored_relpath")
            if not isinstance(source_relpath, str) or not source_relpath.strip():
                raise ValueError(f"missing copied source for task {task_id}")
            if not isinstance(stored_relpath, str) or not stored_relpath.strip():
                raise ValueError(f"missing copied stored_relpath for task {task_id}")
            source = (patch_path.parent / source_relpath).resolve()
            source.relative_to(patch_path.parent.resolve())
            destination = (task_dir / stored_relpath).resolve()
            destination.relative_to(task_dir.resolve())
            _copy_patch_file(source, destination)

        _validate_task(task_dir, metadata)
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        patched.append(task_id)
    return patched
