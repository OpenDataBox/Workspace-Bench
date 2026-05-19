<div align="center">
  <img src="assets/Workspace-Bench-icon.png" alt="Workspace-Bench" width="240" />
</div>

<div align="center">
  <h1>Workspace-Bench</h1>
  <h3>Benchmarking AI Agents on Workspace Tasks with Large-Scale File Dependencies</h3>
</div>

<div align="center">
  <a href="https://workspace-bench.github.io/">
    <img alt="Project Page" src="https://img.shields.io/badge/Project%20Page-Workspace--Bench-4c6ef5?logo=googlechrome&logoColor=white" />
  </a>
  <a href="https://huggingface.co/datasets/Workspace-Bench/Workspace-Bench">
    <img alt="Dataset" src="https://img.shields.io/badge/Hugging%20Face-Full%20Dataset-ff9f1c?logo=huggingface&logoColor=white" />
  </a>
  <a href="https://huggingface.co/datasets/Workspace-Bench/Workspace-Bench-Lite">
    <img alt="Lite Dataset" src="https://img.shields.io/badge/Hugging%20Face-Lite%20Dataset-f76707?logo=huggingface&logoColor=white" />
  </a>
  <a href="https://arxiv.org/abs/2605.03596">
    <img alt="arXiv Paper" src="https://img.shields.io/badge/arXiv-2605.03596-b31b1b?logo=arxiv&logoColor=white" />
  </a>
  <a href="https://opendatabox.github.io/Workspace-Bench/">
    <img alt="Documentation" src="https://img.shields.io/badge/Docs-MkDocs-4c6ef5?logo=readthedocs&logoColor=white" />
  </a>
</div>

<!-- <div align="center">
  <a href="#overview">Overview</a> •
  <a href="#leaderboard">LeaderBoard</a> •
  <a href="#dataset-introduction">Dataset Introduction</a> •
  <a href="#quick-start">Quick Start</a> •
  <a href="#arxiv-link">arXiv</a>
</div> -->

<br />

## 📰 News
* **[May 07, 2025]**: The full datasets of Version 1.0 are released ([homepage](https://workspace-bench.github.io/), [huggingface](https://huggingface.co/Workspace-Bench))!

## 👋 Overview

Workspace-Bench is a benchmark for evaluating AI agents on **workspace tasks with large-scale file dependencies**. It is built to study a capability we call **Workspace Learning**: whether an agent can identify, reason over, exploit, and update explicit and implicit dependencies among heterogeneous files in a real worker's workspace.

<div align="center">
  <img src="assets/Frameworkv2.png" alt="Workspace-Bench framework overview" width="980" />
</div>

<!-- Unlike benchmarks that either place all information directly in the prompt or provide a small bundle of task-specific files, Workspace-Bench evaluates agents in realistic workspaces where they must independently explore directories, locate relevant evidence, understand cross-file relations, and produce correct deliverables. The benchmark is centered on real-world workplace behavior rather than isolated tool-use or single-file question answering. -->

<!-- The figure above illustrates the overall design of Workspace-Bench. Agents are placed into role-specific workspaces with realistic cross-file dependent tasks, and are evaluated with capability-oriented rubrics that measure not only final correctness but also the ability to navigate complex workspace structure and file relations. -->

## 💫 LeaderBoard

<div align="center">
  <img src="assets/rubrics_success.png" alt="Rubrics success rate across agent settings" width="980" />
</div>

Rubric pass rates on Workspace-Bench-Lite across multiple combinations of agent harnesses and backbone LLMs [See Details](https://workspace-bench.github.io/leaderboard.html).

<!-- LLMs.
It highlights that strong foundation models matter, but harness design still plays a major role in efficiency, cost, and final performance.
Detailed leaderboard tables, per-model breakdowns, and additional analyses will be released together with the public benchmark release -->

## 💽 Dataset Introduction

Workspace-Bench contains:

<div align="center">
  <img src="assets/Distribution.png" alt="Workspace-Bench dataset distribution" width="980" />
</div>

- **5** realistic worker profiles: Operations Manager, Logistics Manager, AI Product Manager, Researcher, and Backend Developer
- **74** file types across heterogeneous workspace environments
- **20,476** files, with workspaces scaling up to **20GB**
- **388** tasks, each paired with an explicit file dependency graph
- **7,399** fine-grained rubrics for evaluation
- **Workspace-Bench-Lite**, a 100-task subset that preserves the benchmark distribution while reducing evaluation cost by about **70%**



<!-- The distribution figure summarizes the current benchmark composition from several perspectives: file types, task abilities, task difficulty, workspace allocation, rubric counts, required files per task, and dependency edge counts. -->
<!-- These statistics reflect the diversity and complexity of Workspace-Bench and show that the benchmark is not limited to a single file format, workspace style, or task pattern. -->
<!-- In particular, Workspace-Bench covers multiple professional roles and difficulty levels, while preserving rich inter-file dependency structures that are essential for realistic workspace evaluation. -->

## 🚀 Quick Start

Follow these steps to download Workspace-Bench-Lite and run a one-task smoke
evaluation.

### Prerequisites

- Docker
- Python 3
- API credentials for the agent you want to run
- An Anthropic-compatible API endpoint for the judge model

```bash
cd evaluation
cp .env.example .env
```

Fill `.env` before running an evaluation. For the default smoke command below,
set `KIMIK25_BASE_URL` and `KIMIK25_API_KEY`. For rubric judging, also set
`JUDGE_BASE_URL`, `JUDGE_MODEL`, and `JUDGE_API_KEY`; the judge endpoint must
be Anthropic-compatible because `agent_as_a_judge.py` runs the judge through
the ClaudeCode harness.

### Download Data

Download the Lite task set and workspace files:

```bash
python3 scripts/download_hf_assets.py --lite --workspaces
```

### Build Environment

```bash
docker compose -f docker/docker-compose.yaml build
docker compose -f docker/docker-compose.yaml run --rm workspace-bench \
  bash /workspace/Workspace-Bench/evaluation/docker/bootstrap.sh
```

### Run One Task

Run a single-task smoke evaluation with Codex:

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

This report only checks whether the agent run completed and produced the
expected output files. To score the task against its rubrics, run
`agent_as_a_judge.py` inside Docker:

```bash
docker compose -f docker/docker-compose.yaml run --rm workspace-bench \
  python3 -u /workspace/Workspace-Bench/evaluation/src/agent_as_a_judge.py \
  --task-dir /workspace/Workspace-Bench/evaluation/output/Codex--Kimi-K2.5--Smoke \
  --eval-yaml /workspace/Workspace-Bench/evaluation/runs/judge.yaml \
  --overwrite
```

Rubric judgments are written into each task directory as:

```text
evaluation/output/Codex--Kimi-K2.5--Smoke/100/rubrics_judge--{JUDGE_MODEL}.json
```

Task outputs, logs, and judge artifacts are written to:

```text
evaluation/output/Codex--Kimi-K2.5--Smoke/
```

### Run Workspace-Bench-Lite

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

### Other Run Configs

You can change the harness, model, and dataset split from the command line:

```bash
docker compose -f docker/docker-compose.yaml run --rm workspace-bench \
  bash /workspace/Workspace-Bench/evaluation/docker/run-benchmark.sh \
  --harness openclaw \
  --model glm-5.1 \
  --dataset lite
```

Supported harness values are `codex`, `openclaw`, `deepagent`, and
`claudecode`. Common model aliases include `gpt-5.4`, `gemini-3.1-pro`,
`kimi-k2.5`, `glm-5.1`, `minimax-m2.7`, `grok-4.3`, and `qwen-3.6`.
When using `claudecode`, the selected model endpoint must be compatible with
the Anthropic API.
For a custom provider model, add `--model-id`, `--model-name`, and
`--env-prefix`.
Completed run outputs are stored under `evaluation/output/`.

### Run the Full Benchmark

Download the full task set:

```bash
python3 scripts/download_hf_assets.py --full
```

Then run the full benchmark:

```bash
docker compose -f docker/docker-compose.yaml run --rm workspace-bench \
  bash /workspace/Workspace-Bench/evaluation/docker/run-benchmark.sh \
  --harness codex \
  --model kimi-k2.5 \
  --dataset full
```

### Visualize Results

To browse runs and rubric judgments in the web dashboard (requires Node.js):

```bash
cd viz
npm install
npm run dev
```

The dashboard will be available at `http://localhost:5173` and automatically discovers results under `evaluation/output/`.

## 🔎 Publications
- [Workspace-Bench 1.0: Benchmarking AI Agents on Workspace Tasks with Large-Scale File Dependencies](https://arxiv.org/abs/2605.03596)


```bibtex
@misc{tang2026workspacebench10benchmarkingai,
      title={Workspace-Bench 1.0: Benchmarking AI Agents on Workspace Tasks with Large-Scale File Dependencies}, 
      author={Zirui Tang and Xuanhe Zhou and Yumou Liu and Linchun Li and Weizheng Wang and Hongzhang Huang and Jun Zhou and Jiachen Song and Shaoli Yu and Jinqi Wang and Zihang Zhou and Hongyi Zhou and Yuting Lv and Jinyang Li and Jiashuo Liu and Ruoyu Chen and Chunwei Liu and GuoLiang Li and Jihua Kang and Fan Wu},
      year={2026},
      eprint={2605.03596},
      archivePrefix={arXiv},
      primaryClass={cs.AI},
      url={https://arxiv.org/abs/2605.03596}
}
```

## 🤝 Acknowledgement

<p align="left">
  <a href="https://www.larksuite.com/" target="_blank">
    <img src="assets/lark-logo.png" alt="Lark / Feishu" height="90">
  </a>
  &nbsp;&nbsp;&nbsp;&nbsp;
  <a href="https://www.sjtu.edu.cn/" target="_blank">
    <img src="assets/sjtu-logo.png" alt="Shanghai Jiao Tong University" height="90">
  </a>
</p>
