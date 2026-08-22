---
name: xlsx
description: "Create, read, edit, recalculate, and visually verify Excel XLSX/XLS/CSV/TSV files. Use whenever a task involves spreadsheets, Excel formulas, tables, CSV data, or workbook output."
compatibility: "Workspace-Bench Office runtime: openpyxl, pandas, NumPy, MarkItDown, LibreOffice Calc, and Poppler."
metadata:
  workspace-bench: office
---

# Spreadsheet workflow

Use this skill for `.xlsx`, `.xls`, `.csv`, `.tsv`, Excel, and spreadsheet
tasks.

## Tool selection

- **Read, transform, and analyze data:** `pandas` and `numpy`.
- **Edit existing XLSX or create formatted workbooks:** `openpyxl`.
- **Create a workbook with JavaScript:** the preinstalled `docx`/`pptxgenjs`
  runtime is not an Excel replacement; use a task-provided artifact runtime if
  one is available, otherwise use `openpyxl`.
- **Legacy XLS:** read with `pandas`/`xlrd`, then save an XLSX when edits are
  required.

## Formula and visual QA

LibreOffice Calc is the formula engine for final verification:

```bash
profile="$(mktemp -d)"
mkdir -p recalc pdf
soffice --headless "-env:UserInstallation=file://$profile" \
  --convert-to xlsx --outdir recalc output.xlsx
soffice --headless "-env:UserInstallation=file://$profile" \
  --convert-to pdf --outdir pdf recalc/output.xlsx
pdftoppm -png -r 144 pdf/output.pdf pdf/page
```

Use the recalculated workbook as the final artifact only after checking that it
did not lose required features. Inspect formula cells for errors such as
`#REF!`, `#DIV/0!`, `#VALUE!`, and `#NAME?`, and visually inspect rendered
pages for truncated columns, bad number formats, and broken charts.

## Authoring rules

- Keep calculations auditable: place formulas in cells instead of only in
  scripts whenever the workbook is meant to be edited by users.
- Apply appropriate number/date formats, column widths, freeze panes, and
  filters when they improve usability.
- Never overwrite the source workbook unless the task explicitly asks for it.
