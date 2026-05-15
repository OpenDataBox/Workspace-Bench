# Workspace-Bench

<div align="center">
  <img src="assets/brand.svg" alt="Workspace-Bench Brand" width="480" />
</div>

Workspace-Bench is a benchmark for evaluating AI agents on **workspace tasks with large-scale file dependencies**. It is built to study a capability we call **Workspace Learning**: whether an agent can identify, reason over, exploit, and update explicit and implicit dependencies among heterogeneous files in a real worker's workspace.

Unlike benchmarks that place all information directly in the prompt or provide a small bundle of task-specific files, Workspace-Bench evaluates agents in realistic workspaces where they must independently explore directories, locate relevant evidence, understand cross-file relations, and produce correct deliverables.

![Workspace-Bench framework overview](assets/Frameworkv2.png)

## What is Workspace Learning?

**Workspace Learning** is the ability of an AI agent to:

1. **Identify** explicit and implicit dependencies among files in a workspace
2. **Reason** over heterogeneous data formats (documents, spreadsheets, code, images, etc.)
3. **Exploit** cross-file relationships to complete multi-step tasks
4. **Update** existing files while preserving consistency across the workspace

## Key Statistics

- **5** realistic worker profiles
- **74** file types across heterogeneous environments
- **20,476** files, with workspaces up to **20GB**
- **388** tasks, each with an explicit file dependency graph
- **7,399** fine-grained rubrics for evaluation
- **Workspace-Bench-Lite**: a 100-task subset reducing cost by ~70%

## The SWE-bench Connection

Workspace-Bench shares design philosophy with [SWE-bench](https://www.swebench.com/): both evaluate agents on real-world tasks with concrete success criteria. While SWE-bench focuses on resolving GitHub issues through code patches, Workspace-Bench focuses on cross-file reasoning and production of heterogeneous deliverables in realistic workplace environments.

## Next Steps

- [Quick Start](quickstart.md) — Run your first evaluation in minutes
- [Dataset](dataset.md) — Understand the task distribution and formats
- [Evaluation](evaluation.md) — Learn how to run full benchmarks and interpret results
