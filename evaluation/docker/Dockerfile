FROM node:24-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV WORKSPACE_BENCH_ROOT=/workspace/Workspace-Bench
ENV WORKSPACE_BENCH_EVAL_ROOT=/workspace/Workspace-Bench/evaluation
ENV RIP_BENCH_ROOT=/workspace/Workspace-Bench
ENV RIP_BENCH_EVAL_ROOT=/workspace/Workspace-Bench/evaluation
ENV CODEX_SANDBOX_MODE=danger-full-access

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    git \
    python3 \
    python3-pip \
    python3-venv \
    python-is-python3 \
    unzip \
    wget \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --no-cache-dir --break-system-packages \
    tqdm \
    pyyaml \
    uv \
    deepseek-harness-sdk==0.1.0rc7

RUN test "$(python3 -c 'from importlib.metadata import version; print(version("deepseek-harness-sdk"))')" = "0.1.0rc7"

RUN npm install -g openclaw @openai/codex@0.130.0

WORKDIR /workspace/Workspace-Bench/evaluation

CMD ["/bin/bash"]
