---
name: pptx
description: "Create, read, edit, convert, and visually verify PowerPoint PPTX/POTX presentations. Use whenever a task mentions slides, a presentation, a deck, PPTX, or POTX."
compatibility: "Workspace-Bench Office runtime: pptxgenjs, MarkItDown PPTX support, LibreOffice, Poppler, Pillow, Graphviz, and optional React/Sharp icon rendering."
metadata:
  workspace-bench: office
---

# PPTX workflow

Use this skill for `.pptx`, `.potx`, presentation, deck, and slides tasks.

## Choose the right approach

- **Read/extract:** use `markitdown input.pptx` and inspect slide thumbnails.
- **Create:** use the preinstalled Node `pptxgenjs` package.
- **Edit a simple deck:** use `pptxgenjs` when rebuilding is acceptable.
- **Edit a template or preserve complex native content:** unpack the PPTX,
  modify OOXML conservatively, then validate and render it.

## Render and inspect every output deck

```bash
profile="$(mktemp -d)"
mkdir -p rendered
soffice --headless "-env:UserInstallation=file://$profile" \
  --convert-to pdf --outdir rendered output.pptx
pdftoppm -png -r 144 rendered/output.pdf rendered/slide
```

Review all rendered slides for clipped text, off-canvas objects, missing fonts,
misaligned images, unreadable charts, and broken themes.

## Authoring rules

- Use native editable shapes, tables, and charts where possible.
- Set a slide layout before placing content and keep all coordinates within the
  canvas.
- Use Graphviz only when a diagram is truly needed; render it to a high-quality
  image before inserting it.
- If React/Sharp are used for icons, rasterize the SVG at a sufficiently high
  resolution before embedding it.
- Keep temporary previews and scripts outside the requested output directory.
