---
name: pdfs
description: "Use for any PDF reading, creation, editing, form, OCR, table-extraction, or visual-verification task."
compatibility: "Workspace-Bench Codex runtime: reportlab, pdfplumber, pypdf, Poppler, qpdf, and Tesseract OCR."
metadata:
  workspace-bench: office
---

# PDF workflow

Use this skill for every `.pdf` task.

## Tool selection

- Use `pypdf` for pages, metadata, merging, splitting, rotation, encryption,
  and basic form operations.
- Use `pdfplumber` for layout-aware text and table extraction.
- Use `reportlab` to create new PDFs.
- Use Poppler (`pdfinfo`, `pdftotext`, `pdftoppm`, `pdftocairo`) for inspection
  and rendering.
- Use `qpdf` for command-line merge/split/repair/decryption operations.
- For image-only PDFs, use `pdf2image` plus `pytesseract`.

## Required visual verification

After any create or edit operation, render and inspect the output:

```bash
mkdir -p rendered
pdftoppm -png -r 144 output.pdf rendered/page
pdfinfo output.pdf
```

Text extraction alone does not prove layout fidelity. Check the images for
clipping, overlap, blank pages, unreadable tables, and font substitution.

## Delivery rules

- Preserve the source file unless replacement is explicitly requested.
- Verify page count and openability before delivery.
- Do not flatten interactive forms unless the task explicitly requests a static
  document.
