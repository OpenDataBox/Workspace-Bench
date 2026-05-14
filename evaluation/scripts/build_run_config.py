#!/usr/bin/env python3
import argparse
import json
import re
from pathlib import Path
from typing import Any

import yaml


Json = Any

ROLE_DIRS = {
    "产品人员": "chanpin",
    "开发人员": "kaifa",
    "研究人员": "research",
    "运营人员": "yunying",
    "行政/后勤人员": "houqin",
}

MODEL_ALIASES = {
    "gpt-5.4": ("GPT-5.4", "gpt-5.4", "GPT54"),
    "gemini-3.1-pro": ("Gemini-3.1-Pro", "gemini-3.1-pro-preview", "GEMINI31PRO"),
    "kimi-k2.5": ("Kimi-K2.5", "kimi-k2.5", "KIMIK25"),
    "glm-5.1": ("GLM-5.1", "glm-5.1", "GLM51"),
    "minimax-m2.7": ("MiniMax-M2.7", "MiniMax-M2.7", "MINIMAXM27"),
    "grok-4.3": ("Grok-4.3", "x-ai/grok-4.3", "GROK43"),
    "qwen-3.6": ("Qwen-3.6", "qwen/qwen3.6-35b-a3b", "QWEN36"),
}

PROMPT_TAIL = (
    "[Note] Save all task deliverables to the required location inside the working directory. "
    "When you finish, provide the final output file paths as a list. "
    "The paths must be relative to the working directory, for example "
    "['model_output/a.xlsx', 'model_output/b.docx']."
)


def _safe_slug(value: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return s.strip("-").lower() or "custom"


def _display_slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()) or "Custom"


def _normalize_harness(value: str) -> str:
    mapping = {
        "codex": "Codex",
        "openclaw": "OpenClaw",
        "deepagent": "DeepAgent",
        "claudecode": "ClaudeCode",
        "claude-code": "ClaudeCode",
    }
    key = value.strip().lower()
    if key not in mapping:
        raise SystemExit(f"unsupported harness: {value}")
    return mapping[key]


def _model_info(model: str, model_id: str | None, model_name: str | None, env_prefix: str | None) -> tuple[str, str, str, str]:
    key = model.strip().lower()
    default_name, default_id, default_env = MODEL_ALIASES.get(
        key,
        (_display_slug(model), model, re.sub(r"[^A-Za-z0-9]+", "_", model).upper()),
    )
    display_name = model_name or default_name
    llm_model = model_id or default_id
    env = env_prefix or default_env
    return key, display_name, llm_model, env


def _provider_config(harness: str, provider_type: str, env_prefix: str, llm_model: str) -> dict[str, str]:
    if harness == "ClaudeCode" or provider_type == "anthropic":
        return {
            "provider_type": "anthropic",
            "baseUrl": f"${{{env_prefix}_ANTHROPIC_BASE_URL:-${{{env_prefix}_BASE_URL}}}}",
            "model": f"${{{env_prefix}_ANTHROPIC_MODEL:-{llm_model}}}",
            "apiKey": f"${{{env_prefix}_API_KEY}}",
        }
    return {
        "provider_type": provider_type,
        "baseUrl": f"${{{env_prefix}_BASE_URL}}",
        "model": llm_model,
        "apiKey": f"${{{env_prefix}_API_KEY}}",
    }


def _fs_map(eval_root: Path, harness: str, model_name: str) -> dict[str, dict[str, str]]:
    suffix = f"{harness}_{_display_slug(model_name)}"
    return {
        "raw_work_dir": {role: f"filesys/{prefix}_raw" for role, prefix in ROLE_DIRS.items()},
        "standard_work_dir": {role: f"filesys/{prefix}_standard" for role, prefix in ROLE_DIRS.items()},
        "work_dir": {role: f"filesys/{prefix}_workdir_{suffix}" for role, prefix in ROLE_DIRS.items()},
    }


def build_config(args: argparse.Namespace) -> Path:
    eval_root = Path(args.eval_root).resolve()
    harness = _normalize_harness(args.harness)
    model_key, model_name, llm_model, env_prefix = _model_info(
        args.model,
        args.model_id,
        args.model_name,
        args.env_prefix,
    )

    dataset = args.dataset.strip().lower()
    if dataset not in {"smoke", "lite", "full"}:
        raise SystemExit(f"unsupported dataset: {args.dataset}")

    run_name = args.run_name or {"smoke": "Smoke", "lite": "Lite", "full": "Full"}[dataset]
    task_path = eval_root / ("tasks" if dataset == "full" else "tasks_lite")
    task_limit = args.task_limit
    if task_limit is None and dataset == "smoke":
        task_limit = 1

    generated_root = eval_root / ".generated" / "run_configs"
    runs_dir = generated_root / "runs"
    fs_map_dir = generated_root / "fs_map"
    runs_dir.mkdir(parents=True, exist_ok=True)
    fs_map_dir.mkdir(parents=True, exist_ok=True)

    config_slug = f"{harness.lower()}-{_safe_slug(args.model)}-{dataset}"
    fs_map_path = fs_map_dir / f"fs_map_{harness}_{_display_slug(model_name)}.json"
    fs_map_path.write_text(
        json.dumps(_fs_map(eval_root, harness, model_name), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    config: dict[str, Json] = {
        "agent_name": harness,
        "model_name": model_name,
        "run_name": run_name,
        "task_path": str(task_path),
        "output_dir": str(eval_root / "output"),
        "fs_map_file": str(fs_map_path),
        "prompt_head": None,
        "prompt_tail": PROMPT_TAIL,
        "timeout_sec": float(args.timeout_sec),
        "task_target_output_dir": "model_output",
        "eval_while_running": False,
        "eval_yaml": args.eval_yaml,
        "api_provider": _provider_config(harness, args.provider_type, env_prefix, llm_model),
    }
    if task_limit is not None:
        config["task_limit"] = int(task_limit)

    config_path = runs_dir / f"{config_slug}.yaml"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return config_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a Workspace-Bench run config from parameters.")
    parser.add_argument("--harness", required=True, help="Codex, OpenClaw, DeepAgent, or ClaudeCode")
    parser.add_argument("--model", required=True, help="Model alias or custom model id")
    parser.add_argument("--dataset", default="lite", choices=["smoke", "lite", "full"])
    parser.add_argument("--eval-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--provider-type", default="openai", choices=["openai", "anthropic"])
    parser.add_argument("--model-id", help="LLM provider model id; defaults from --model")
    parser.add_argument("--model-name", help="Display name used in output directory")
    parser.add_argument("--env-prefix", help="Environment variable prefix for BASE_URL/API_KEY")
    parser.add_argument("--run-name", help="Output run name; defaults to Smoke/Lite/Full")
    parser.add_argument("--task-limit", type=int)
    parser.add_argument("--timeout-sec", type=float, default=2000.0)
    parser.add_argument("--eval-yaml", default="runs/judge.yaml")
    args = parser.parse_args()
    print(build_config(args))


if __name__ == "__main__":
    main()
