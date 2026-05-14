# Dataset

Workspace-Bench contains realistic workspace tasks designed to evaluate an agent's ability to understand and manipulate large-scale file dependencies.

## Overview

![Dataset distribution](assets/Distribution.png)

## Worker Profiles

Tasks are organized around **5 realistic worker profiles**, each with distinct workspace environments and file types:

| Profile | Role | Typical Files |
|---------|------|---------------|
| Operations Manager (`yunying`) | Event planning, process management | Docs, spreadsheets, presentations |
| Logistics Manager (`houqin`) | Supply chain, vendor coordination | Contracts, schedules, budgets |
| AI Product Manager (`chanpin`) | Product specs, user research | PRDs, mockups, roadmaps |
| Researcher (`research`) | Academic writing, data analysis | Papers, notebooks, datasets |
| Backend Developer (`kaifa`) | API design, database schemas | Code, configs, SQL |

## Task Structure

Each task consists of:

- **`metadata.json`** — Task description, expected outputs, and rubrics
- **`data/`** — Input files that populate the workspace
- **File Dependency Graph** — Explicit `from -> to` relationships between files
- **Rubrics** — Fine-grained evaluation criteria (7,399 total across all tasks)

### Metadata Format

```json
{
  "absolute_id": 100,
  "persona": "Logistics Manager",
  "task": "Integrate the contents of four files and organize a complete onsite_hosting_execution_manual.doc...",
  "task_diff": "medium",
  "output_files": ["onsite_hosting_execution_manual.doc"],
  "rubrics": [
    "In onsite_hosting_execution_manual.doc, is the hosting content for the warm-up and opening section complete..."
  ],
  "rubric_types": ["Process Evaluation", "Outcome Evaluation"],
  "file_dep_graph": [
    {"from": "host_script_1.docx", "to": "onsite_hosting_execution_manual.doc"}
  ],
  "data_manifest": [
    {"filename": "host_script_1.docx", "stored_relpath": "data/a60fb401fab41412_host_script_1.docx"}
  ]
}
```

## Dataset Splits

### Workspace-Bench-Lite

A curated **100-task subset** that preserves the full benchmark's distribution across personas, difficulties, and file types while reducing evaluation cost by approximately **70%**.

```bash
python3 scripts/download_hf_assets.py --lite --workspaces
```

### Full Workspace-Bench

The complete **388-task** dataset with all workspaces.

```bash
python3 scripts/download_hf_assets.py --full
```

## File Types

Workspace-Bench spans **74 file types**, including but not limited to:

- Documents: `.doc`, `.docx`, `.pdf`, `.md`, `.txt`
- Spreadsheets: `.xls`, `.xlsx`, `.csv`
- Presentations: `.ppt`, `.pptx`
- Code: `.py`, `.js`, `.sql`, `.yaml`, `.json`
- Images: `.png`, `.jpg`, `.webp`
- Archives: `.zip`

## Workspace Scale

- **Total files**: 20,476
- **Max workspace size**: up to 20GB
- **Tasks per persona**: ~60-100
- **Average files per task**: ~50-200

## Accessing the Datasets

Datasets are hosted on Hugging Face:

- [Full Dataset](https://huggingface.co/datasets/Workspace-Bench/Workspace-Bench)
- [Lite Dataset](https://huggingface.co/datasets/Workspace-Bench/Workspace-Bench-Lite)

You can also load them programmatically:

```python
from datasets import load_dataset

lite = load_dataset("Workspace-Bench/Workspace-Bench-Lite", split="test")
```
