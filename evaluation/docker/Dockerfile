FROM node:24-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV WORKSPACE_BENCH_ROOT=/workspace/Workspace-Bench
ENV WORKSPACE_BENCH_EVAL_ROOT=/workspace/Workspace-Bench/evaluation
ENV RIP_BENCH_ROOT=/workspace/Workspace-Bench
ENV RIP_BENCH_EVAL_ROOT=/workspace/Workspace-Bench/evaluation
ENV CODEX_SANDBOX_MODE=danger-full-access
ENV WORKSPACE_BENCH_OFFICE_RUNTIME=/opt/workspace-bench/office-runtime
ENV WORKSPACE_BENCH_OFFICE_SKILLS=/opt/workspace-bench/office-skills
ENV CODEX_HOME=/opt/workspace-bench/agent-homes/codex/.codex
ENV WORKSPACE_BENCH_CODEX_HOME_TEMPLATE=/opt/workspace-bench/agent-homes/codex/.codex
ENV WORKSPACE_BENCH_CLAUDE_CONFIG_DIR=/opt/workspace-bench/agent-homes/claude/.claude
ENV WORKSPACE_BENCH_OPENCLAW_SKILLS_DIR=/opt/workspace-bench/agent-homes/openclaw/.openclaw/skills
ENV WORKSPACE_BENCH_DEEPAGENTS_SKILLS_DIR=/opt/workspace-bench/agent-homes/deepagents/.deepagents/skills
ENV NODE_PATH=/opt/workspace-bench/office-runtime/node_modules

RUN apt-get update && apt-get install -y --no-install-recommends \
    antiword \
    bash \
    ca-certificates \
    curl \
    fonts-noto-cjk \
    fonts-noto-core \
    ghostscript \
    git \
    graphviz \
    libreoffice-calc \
    libreoffice-common \
    libreoffice-core \
    libreoffice-impress \
    libreoffice-writer \
    pandoc \
    pdftk-java \
    poppler-utils \
    python3 \
    python3-pip \
    python3-venv \
    python-is-python3 \
    qpdf \
    tesseract-ocr \
    tesseract-ocr-chi-sim \
    tesseract-ocr-eng \
    unzip \
    wget \
    zip \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --no-cache-dir --break-system-packages \
    defusedxml \
    lxml \
    markitdown[pptx] \
    numpy \
    openpyxl \
    pandas \
    pdf2image \
    pdfplumber \
    pypdf \
    pypdfium2 \
    Pillow \
    python-docx \
    python-pptx \
    pytesseract \
    reportlab \
    tqdm \
    pyyaml \
    uv \
    deepseek-harness-sdk==0.1.0rc7

RUN test "$(python3 -c 'from importlib.metadata import version; print(version("deepseek-harness-sdk"))')" = "0.1.0rc7"

RUN npm install -g openclaw @openai/codex@0.130.0

COPY evaluation/docker/office-runtime.package.json /opt/workspace-bench/office-runtime/package.json
RUN npm install --prefix "$WORKSPACE_BENCH_OFFICE_RUNTIME" --omit=dev

# Each harness receives the same maintained skill pack in its native discovery
# location.  The task service changes HOME to a disposable tmpfs directory, so
# CODEX_HOME/CLAUDE_CONFIG_DIR and the harness adapters point at these immutable
# image locations instead of a task-local empty home directory.
COPY evaluation/skills/office/ /opt/workspace-bench/office-skills/
RUN mkdir -p \
      "$CODEX_HOME/skills" \
      "$WORKSPACE_BENCH_CLAUDE_CONFIG_DIR/skills" \
      "$WORKSPACE_BENCH_OPENCLAW_SKILLS_DIR" \
      "$WORKSPACE_BENCH_DEEPAGENTS_SKILLS_DIR" \
    && for skill in docx pdfs slides spreadsheets; do \
         cp -a "$WORKSPACE_BENCH_OFFICE_SKILLS/$skill" "$CODEX_HOME/skills/"; \
       done \
    && for skill in docx pdf pptx xlsx; do \
         cp -a "$WORKSPACE_BENCH_OFFICE_SKILLS/$skill" "$WORKSPACE_BENCH_CLAUDE_CONFIG_DIR/skills/"; \
       done \
    && cp -a "$WORKSPACE_BENCH_OFFICE_SKILLS/." "$WORKSPACE_BENCH_OPENCLAW_SKILLS_DIR/" \
    && cp -a "$WORKSPACE_BENCH_OFFICE_SKILLS/." "$WORKSPACE_BENCH_DEEPAGENTS_SKILLS_DIR/"

RUN command -v soffice \
    && command -v pandoc \
    && command -v pdfinfo \
    && command -v pdftoppm \
    && command -v qpdf \
    && command -v tesseract \
    && command -v dot \
    && python3 - <<'PY'
import importlib

for module in (
    "docx",
    "openpyxl",
    "pandas",
    "numpy",
    "pdf2image",
    "pdfplumber",
    "pypdf",
    "pptx",
    "reportlab",
):
    importlib.import_module(module)
PY
RUN node -e 'for (const name of ["docx", "pdf-lib", "pptxgenjs", "react", "react-dom", "react-icons", "sharp"]) require(name)'
RUN for skill in docx pdfs slides spreadsheets; do \
      test -f "$CODEX_HOME/skills/$skill/SKILL.md"; \
    done \
    && for skill in docx pdf pptx xlsx; do \
      test -f "$WORKSPACE_BENCH_CLAUDE_CONFIG_DIR/skills/$skill/SKILL.md"; \
    done \
    && for skill in docx pdf pdfs pptx slides xlsx spreadsheets; do \
      test -f "$WORKSPACE_BENCH_OPENCLAW_SKILLS_DIR/$skill/SKILL.md"; \
      test -f "$WORKSPACE_BENCH_DEEPAGENTS_SKILLS_DIR/$skill/SKILL.md"; \
    done

WORKDIR /workspace/Workspace-Bench/evaluation

CMD ["/bin/bash"]
