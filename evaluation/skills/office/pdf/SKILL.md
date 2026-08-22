---
name: pdf
description: "Read, create, edit, merge, split, render, OCR, and verify PDF files. Use whenever a task involves PDF input or output, including forms, scanned documents, table extraction, or visual-layout checks."
compatibility: "Workspace-Bench Office runtime: pypdf, pdfplumber, reportlab, Poppler, qpdf, and Tesseract OCR."
metadata:
  workspace-bench: office
---

# PDF workflow

Use this skill for every `.pdf` task.

## Tool selection

- **Text, metadata, page manipulation:** `pypdf`.
- **Text with layout and tables:** `pdfplumber`.
- **New PDFs:** `reportlab`.
- **Render/inspect pages:** Poppler (`pdfinfo`, `pdftotext`, `pdftoppm`,
  `pdftocairo`).
- **Merge, split, rotate, decrypt, repair:** `qpdf`.
- **Scanned pages / OCR:** `pytesseract` with `pdf2image` or Poppler images.

## Visual QA is required for authored or modified PDFs

```bash
mkdir -p rendered
pdftoppm -png -r 144 document.pdf rendered/page
pdfinfo document.pdf
```

Inspect every page image for clipping, overlap, font substitution, blank pages,
and unreadable tables. Text extraction alone is not a layout check.

## OCR

For image-only PDFs, render at a sufficiently high resolution and use
`pytesseract`. The image should be deskewed or otherwise cleaned when OCR
quality is poor. Clearly distinguish OCR-derived text from native PDF text.

## Delivery rules

- Preserve the original unless replacement was explicitly requested.
- Validate output page count and openability with `pdfinfo` or `pypdf`.
- Do not flatten interactive forms unless the task explicitly requests a static
  final document.
