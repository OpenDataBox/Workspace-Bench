# Quick Start Guide

This guide will help you get started with Workspace-Bench, from installation to running your first evaluation.

## Prerequisites

- Docker
- Python 3
- API credentials for the agent you want to run
- An Anthropic-compatible API endpoint for the judge model
- Node.js (≥ 18, only for the visualization dashboard)

## Setup

First, clone the repository and prepare your environment:

```bash
git clone https://github.com/OpenDataBox/Workspace-Bench.git
cd Workspace-Bench/evaluation
cp .env.example .env
```

Fill `.env` with your API credentials before running an evaluation. For the default smoke command below, set `KIMIK25_BASE_URL` and `KIMIK25_API_KEY`. For rubric judging, also set `JUDGE_BASE_URL`, `JUDGE_MODEL`, and `JUDGE_API_KEY`; the judge endpoint must be Anthropic-compatible because `agent_as_a_judge.py` runs the judge through the ClaudeCode harness.

!!! note "Supported Providers"
    Workspace-Bench supports multiple model providers. See the `.env.example` for the full list of environment variables.

## Download Data

Download the Lite task set and workspace files:

```bash
python3 scripts/download_hf_assets.py --lite --workspaces
```

This will populate `evaluation/tasks_lite/` with task metadata and `evaluation/filesys/` with the corresponding workspace files.

## Build Environment

Build the Docker image and bootstrap the evaluation environment:

```bash
docker compose -f docker/docker-compose.yaml build
docker compose -f docker/docker-compose.yaml run --rm workspace-bench \
  bash /workspace/Workspace-Bench/evaluation/docker/bootstrap.sh
```

## Run One Task (Smoke Test)

Run a single-task smoke evaluation with the Codex harness:

```bash
docker compose -f docker/docker-compose.yaml run --rm workspace-bench \
  bash /workspace/Workspace-Bench/evaluation/docker/run-benchmark.sh \
  --harness codex \
  --model kimi-k2.5 \
  --dataset smoke
```

Check the report:

```bash
python3 scripts/assert_agent_runner_report.py \
  output/Codex--Kimi-K2.5--Smoke/agent_runner_report.json
```

The expected output is:

```text
[ok] output/Codex--Kimi-K2.5--Smoke/agent_runner_report.json: 1/1 passed
```

Task outputs and logs are written to:

```text
evaluation/output/Codex--Kimi-K2.5--Smoke/
```

## Judge Rubrics

The smoke report verifies that the agent run completed and produced output files. To score correctness against the task rubrics, run the judge inside Docker:

```bash
docker compose -f docker/docker-compose.yaml run --rm workspace-bench \
  python3 -u /workspace/Workspace-Bench/evaluation/src/agent_as_a_judge.py \
  --task-dir /workspace/Workspace-Bench/evaluation/output/Codex--Kimi-K2.5--Smoke \
  --eval-yaml /workspace/Workspace-Bench/evaluation/runs/judge.yaml \
  --overwrite
```

Rubric judgments are written into each task directory:

```text
evaluation/output/Codex--Kimi-K2.5--Smoke/100/rubrics_judge--{JUDGE_MODEL}.json
```

## Run Workspace-Bench-Lite

Run the 100-task Lite benchmark:

```bash
docker compose -f docker/docker-compose.yaml run --rm workspace-bench \
  bash /workspace/Workspace-Bench/evaluation/docker/run-benchmark.sh \
  --harness codex \
  --model kimi-k2.5 \
  --dataset lite
```

Then judge the completed Lite run:

```bash
docker compose -f docker/docker-compose.yaml run --rm workspace-bench \
  python3 -u /workspace/Workspace-Bench/evaluation/src/agent_as_a_judge.py \
  --task-dir /workspace/Workspace-Bench/evaluation/output/Codex--Kimi-K2.5--Lite \
  --eval-yaml /workspace/Workspace-Bench/evaluation/runs/judge.yaml \
  --parallel \
  --workers 3
```

## Visualize Results

After running evaluations, start the visualization dashboard:

```bash
cd ../viz
npm install
npm run dev
```

The dashboard will be available at `http://localhost:5173` and automatically discovers results under `evaluation/output/`.

## Next Steps

- [Dataset](dataset.md) — Learn about task formats and the Lite vs Full splits
- [Evaluation](evaluation.md) — Explore advanced evaluation options and multiple harnesses
