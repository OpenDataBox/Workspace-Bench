---
name: docx
description: "Create, read, edit, convert, and visually verify Microsoft Word DOCX/DOTX files. Use whenever a task involves Word documents, templates, tracked changes, comments, document formatting, or DOCX output."
compatibility: "Workspace-Bench Office runtime: Node docx, python-docx, Pandoc, LibreOffice, and Poppler."
metadata:
  workspace-bench: office
---

# DOCX workflow

Use this skill for `.docx`, `.dotx`, and legacy `.doc` files.

## Choose the right approach

- **Read/extract:** use `pandoc -t markdown input.docx`, `python-docx`, or unzip
  the Office Open XML package when structure matters.
- **Create:** use the preinstalled Node `docx` package or `python-docx`.
- **Edit existing DOCX:** use `python-docx` for normal paragraph/table edits. For
  unsupported constructs, unpack the archive, edit the relevant XML carefully,
  and repackage it.
- **Legacy DOC:** convert it with LibreOffice before editing.

## Required quality check

After creating or changing a document, render it to PDF and inspect the pages:

```bash
profile="$(mktemp -d)"
mkdir -p rendered
soffice --headless "-env:UserInstallation=file://$profile" \
  --convert-to pdf --outdir rendered output.docx
pdftoppm -jpeg -r 144 rendered/output.pdf rendered/page
```

Check the rendered images for clipping, missing fonts, page breaks, table
overflow, and unreadable text. Preserve the requested editable DOCX file; the
PDF is a QA artifact unless the task asks for it.

## Practical requirements

- Prefer semantic headings, lists, tables, and styles over manual whitespace.
- Use the installed Noto fonts for Chinese or mixed-language documents.
- Do not assume text extraction proves visual fidelity.
- Keep intermediate scripts, unpacked XML, and rendered previews outside the
  requested output location.
