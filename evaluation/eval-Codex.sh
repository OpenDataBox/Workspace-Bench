#!/usr/bin/env bash
set -euo pipefail

mkdir -p logs

for model in GPT-5.4 Gemini-3.1-Pro Kimi-K2.5 GLM-5.1 MiniMax-M2.7; do
  config="runs/Codex--${model}--Test-Rubrics-Checked.yaml"
  log="logs/Codex-${model}.log"
  python -u src/agent_runner.py --run-config "$config" > "$log" 2>&1 &
  pid=$!
  echo "$pid" >> logs/Codex-PIDs.log
  echo "Codex-${model} started with PID: ${pid}" >> logs/RUN-PIDs.log
  echo "Codex-${model} started with PID: ${pid}"
done
