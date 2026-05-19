# Evaluation Guide

This guide explains how to evaluate AI agents on Workspace-Bench tasks.

## Overview

Workspace-Bench evaluates agents by placing them in realistic workspace environments, providing a task description, and measuring their ability to produce correct outputs against fine-grained rubrics. The evaluation supports multiple agent harnesses and can be run via Docker for reproducibility.

Agent runs and rubric judging are separate steps. `run-benchmark.sh` runs the
selected agent and writes outputs plus `agent_runner_report.json`.
`agent_as_a_judge.py` then evaluates those outputs against the task rubrics
using a judge model through an Anthropic-compatible API.

## Supported Harnesses

| Harness | Description | API Compatibility |
|---------|-------------|-------------------|
| `codex` | OpenAI Codex / Responses API | OpenAI Responses → Chat Completions adapter |
| `openclaw` | OpenClaw agent harness | OpenAI-compatible Chat Completions |
| `deepagent` | DeepAgents harness (LangChain) | OpenAI-compatible |
| `claudecode` | Claude Code harness | Anthropic API |

## Supported Models

Common model aliases include:

- `gpt-5.4`
- `gemini-3.1-pro`
- `kimi-k2.5`
- `glm-5.1`
- `minimax-m2.7`
- `grok-4.3`
- `qwen-3.6`

For a custom provider, add `--model-id`, `--model-name`, and `--env-prefix` to the run command.

## Running Evaluations

### Basic Evaluation on Lite

```bash
docker compose -f docker/docker-compose.yaml run --rm workspace-bench \
  bash /workspace/Workspace-Bench/evaluation/docker/run-benchmark.sh \
  --harness codex \
  --model kimi-k2.5 \
  --dataset lite
```

Then judge the completed run:

```bash
docker compose -f docker/docker-compose.yaml run --rm workspace-bench \
  python3 -u /workspace/Workspace-Bench/evaluation/src/agent_as_a_judge.py \
  --task-dir /workspace/Workspace-Bench/evaluation/output/Codex--Kimi-K2.5--Lite \
  --eval-yaml /workspace/Workspace-Bench/evaluation/runs/judge.yaml \
  --parallel \
  --workers 3
```

`runs/judge.yaml` reads `JUDGE_BASE_URL`, `JUDGE_MODEL`, and `JUDGE_API_KEY`
from `.env`. The judge endpoint must be Anthropic-compatible because the
judge is executed through the ClaudeCode harness.

### Evaluation on the Full Benchmark

```bash
python3 scripts/download_hf_assets.py --full

docker compose -f docker/docker-compose.yaml run --rm workspace-bench \
  bash /workspace/Workspace-Bench/evaluation/docker/run-benchmark.sh \
  --harness codex \
  --model kimi-k2.5 \
  --dataset full
```

### Using Different Harnesses

```bash
# OpenClaw + GLM
docker compose -f docker/docker-compose.yaml run --rm workspace-bench \
  bash /workspace/Workspace-Bench/evaluation/docker/run-benchmark.sh \
  --harness openclaw \
  --model glm-5.1 \
  --dataset lite

# DeepAgent + MiniMax
docker compose -f docker/docker-compose.yaml run --rm workspace-bench \
  bash /workspace/Workspace-Bench/evaluation/docker/run-benchmark.sh \
  --harness deepagent \
  --model minimax-m2.7 \
  --dataset lite
```

## Evaluation Outputs

Completed runs are stored under `evaluation/output/` with the naming convention:

```
{Harness}--{Model}--{Dataset}/
```

Each task directory contains:

- `metadata.json` — Task definition
- `agent.json` — Execution trace, token usage, and status
- `output/` — Files produced by the agent
- `rubrics_judge--{model}.json` — Rubric evaluation results
- `dependency_graph--{model}.json` — Extracted I/O dependency graph

`agent_runner_report.json` is not the final correctness score; it reports
whether the agent execution itself completed. Final correctness comes from the
`rubrics_judge--{model}.json` files produced by `agent_as_a_judge.py`.

## Interpreting Results

The `agent_runner_report.json` at the run root contains:

```json
{
  "summary": {
    "total": 100,
    "passed": 67,
    "failed": 20,
    "error": 8,
    "timeout": 5
  },
  "cases": [...]
}
```

A task is marked **passed** if the agent successfully produced output files. Final correctness is determined by rubric judgment.

### Rubric Judgment

Rubric files contain per-criterion evaluations:

```json
{
  "rubrics": [
    {
      "index": 0,
      "rubric": "Is the output format correct?",
      "passed": true,
      "confidence": 0.95,
      "evidence": "File output.docx contains properly formatted sections..."
    }
  ],
  "summary": {
    "total": 7,
    "passed": 5,
    "failed": 2
  }
}
```

## Advanced Options

### Custom Providers

For models not in the predefined alias list:

```bash
docker compose -f docker/docker-compose.yaml run --rm workspace-bench \
  bash /workspace/Workspace-Bench/evaluation/docker/run-benchmark.sh \
  --harness codex \
  --model-id my-model \
  --model-name My-Model \
  --env-prefix MYMODEL \
  --dataset lite
```

Ensure `MYMODEL_BASE_URL` and `MYMODEL_API_KEY` are set in `.env`.

### Running Without Docker

You can also run evaluations directly if you have the dependencies installed:

```bash
cd evaluation
python3 -m pip install -e requirements.txt  # if available
python3 src/agent_runner.py --run-config runs/my_config.yaml
```

## Next Steps

- [Visualization](visualization.md) — Browse results in the web dashboard
