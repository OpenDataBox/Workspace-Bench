# Visualization

Workspace-Bench includes a built-in visualization dashboard for browsing evaluation results, inspecting task outputs, and analyzing agent performance.

## Getting Started

### Prerequisites

- Node.js ≥ 18

### Install and Run

```bash
cd viz
npm install
npm run dev
```

This starts both the Vite React frontend and the Express API backend concurrently:

- **Frontend**: `http://localhost:5173`
- **API**: `http://localhost:3000`

The dashboard automatically discovers run directories under `evaluation/output/` relative to the project root.

### Build for Production

```bash
npm run build
```

The production build outputs static files to `viz/dist/`.

## Dashboard Pages

### Home

Overview of all completed benchmark runs. Each run card shows:

- Harness and model name
- Dataset split (Smoke / Lite / Full)
- Pass / fail / error / timeout counts
- Total duration

### Run Detail

Drill into a specific run to see:

- **Task list** with per-task status, duration, and token usage
- **Rubric judgment summary** (passed vs failed criteria)
- **Output files** produced by the agent
- **Dependency graph** extracted from tool calls

### File View

Side-by-side file comparison with syntax highlighting (powered by Monaco Editor):

- **Workspace files** — Original inputs from the task
- **Agent outputs** — Files produced by the agent
- **Ground truth** — Reference standard outputs (if available)

### Statistics

Aggregate analysis across runs:

- **Rubric success rate** by task type and difficulty
- **Token histogram** — Prompt vs completion distribution
- **Tool call frequency** — Which tools agents use most

## Tech Stack

- **Frontend**: React 18 + TypeScript + Vite + Tailwind CSS + Zustand
- **Backend**: Express + TypeScript
- **Editor**: Monaco Editor

## Customizing the Data Path

By default, the API server scans `../evaluation/output/` for run directories. To point it elsewhere, set the environment variable before starting the server:

```bash
EVAL_OUTPUT_DIR=/path/to/output npm run server:dev
```
