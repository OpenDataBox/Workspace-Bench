---
name: slides
description: "Use for slide decks, PowerPoint/PPTX/POTX files, and presentation tasks that require creation, editing, rendering, or visual QA."
compatibility: "Workspace-Bench Codex runtime: use a provided @oai/artifact-tool runtime when available; LibreOffice, Poppler, MarkItDown, Pillow, and Graphviz support reading and QA."
metadata:
  workspace-bench: office
---

# Slides workflow

Use this skill for every PPTX/POTX or presentation task.

## Authoring priority

1. If the Codex environment supplies `load_workspace_dependencies` and
   `@oai/artifact-tool`, use that provided runtime for deck authoring; do not
   replace it with a global or repository-local implementation.
2. If the task environment does not supply that Codex artifact runtime, use the
   preinstalled `pptxgenjs` package from `NODE_PATH`.
3. Use `markitdown` for text extraction and inspect template/deck structure
   before modifying an existing presentation.

## Render and inspect every output deck

```bash
profile="$(mktemp -d)"
mkdir -p rendered
soffice --headless "-env:UserInstallation=file://$profile" \
  --convert-to pdf --outdir rendered output.pptx
pdftoppm -png -r 144 rendered/output.pdf rendered/slide
```

Review all rendered slides for clipped text, off-canvas elements, missing
fonts, unreadable charts, alignment errors, and theme regressions.

## Additional rules

- Preserve template structure and native editable objects where possible.
- Use Graphviz only for diagrams that materially improve the result.
- Keep intermediate scripts and preview files outside the requested output
  directory.
