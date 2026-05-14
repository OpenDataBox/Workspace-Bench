import json
from tqdm import tqdm  # 添加 tqdm 导入语句

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import random
import re
import shutil
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import yaml

# We reuse dependency-graph builder and metadata helpers to keep I/O aligned.
import agent_eval as _ae

# ClaudeCode baseline runner (wraps evaluation_sys/baselines/ClaudeCode.js).
from agents import claudecode as _claudecode


Json = Any


def _iso_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _read_yaml(path: str) -> Dict[str, Json]:
    with open(path, "r", encoding="utf-8") as f:
        obj = yaml.safe_load(f)
    return _expand_config_env(obj) if isinstance(obj, dict) else {}


def _expand_env_string(value: str) -> str:
    fallback_re = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)[:-]-(\$\{[A-Za-z_][A-Za-z0-9_]*\}|[^}]*)\}")
    s = value
    while True:
        m = fallback_re.search(s)
        if not m:
            break
        primary = os.environ.get(m.group(1), "")
        fallback = m.group(2)
        repl = primary if primary else os.path.expandvars(fallback)
        s = s[: m.start()] + repl + s[m.end() :]
    return os.path.expandvars(s)


def _expand_config_env(value: Json) -> Json:
    if isinstance(value, str):
        return _expand_env_string(value)
    if isinstance(value, list):
        return [_expand_config_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_config_env(v) for k, v in value.items()}
    return value


def _safe_load_json(path: str) -> Optional[Json]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_json(path: str, obj: Json) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(text or ""))


def _truncate_str(s: str, max_len: int = 2000) -> str:
    if not isinstance(s, str):
        return str(s)
    if len(s) <= max_len:
        return s
    return s[:max_len] + "...[truncated]"


def _json_first_object(text: str) -> Optional[Json]:
    """From a blob of text, extract the first JSON object/array."""
    s = str(text or "").lstrip()
    if not s:
        return None
    pos1 = s.find("{")
    pos2 = s.find("[")
    pos = pos1 if pos2 == -1 else (pos2 if pos1 == -1 else min(pos1, pos2))
    if pos == -1:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(s[pos:])
        return obj
    except Exception:
        return None


def _safe_remove_path(path: str) -> None:
    try:
        if os.path.islink(path) or os.path.isfile(path):
            os.unlink(path)
        elif os.path.isdir(path):
            shutil.rmtree(path)
    except FileNotFoundError:
        return


def _symlink_or_copy(src: str, dst: str) -> None:
    _safe_remove_path(dst)
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    try:
        os.symlink(src, dst)
        return
    except Exception:
        pass
    if os.path.isdir(src):
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)


def _resolve_original_task_source(meta: Dict[str, Json]) -> Optional[str]:
    mp = meta.get("__metadata_path")
    if isinstance(mp, str) and mp.strip():
        d = os.path.dirname(os.path.abspath(mp))
        if os.path.isdir(d):
            return d
    return None


def _prepare_judge_view(*, sandbox_try_dir: str, task_dir: str, meta: Dict[str, Json]) -> Dict[str, str]:
    """
    Build a restricted judge workspace so ClaudeCode can see:
    - original inputs from tasks/<case>/data
    - candidate outputs from task_dir/output
    But it should not see tasks/<case>/output or output_cc (GT-like answers).
    """
    view_dir = os.path.join(sandbox_try_dir, "judge_view")
    os.makedirs(view_dir, exist_ok=True)

    out: Dict[str, str] = {"view_dir": view_dir}

    source_task_dir = _resolve_original_task_source(meta)
    if source_task_dir:
        out["source_task_dir"] = source_task_dir
        inputs_dir = os.path.join(source_task_dir, "data")
        if os.path.isdir(inputs_dir):
            dst = os.path.join(view_dir, "inputs")
            _symlink_or_copy(inputs_dir, dst)
            out["inputs_visible_path"] = dst

        # Copy a sanitized metadata snapshot for context, but do not expose the original task root.
        meta_out_path = os.path.join(view_dir, "original_task_metadata.json")
        _write_json(
            meta_out_path,
            {
                "id": meta.get("id"),
                "task": meta.get("task"),
                "steps": meta.get("steps"),
                "rubrics": meta.get("rubrics"),
                "output_files": meta.get("output_files"),
                "data": meta.get("data"),
                "data_manifest": meta.get("data_manifest"),
                "__metadata_path": meta.get("__metadata_path"),
            },
        )
        out["original_task_metadata_path"] = meta_out_path

    candidate_output_dir = os.path.join(task_dir, "output")
    if os.path.isdir(candidate_output_dir):
        dst = os.path.join(view_dir, "candidate_output")
        _symlink_or_copy(candidate_output_dir, dst)
        out["candidate_output_path"] = dst

    # Also provide a small README to steer the judge away from GT-like dirs.
    readme_path = os.path.join(view_dir, "README.txt")
    _write_text(
        readme_path,
        "\n".join(
            [
                "This is a restricted evaluation workspace for agent-as-a-judge.",
                "",
                "- inputs/: original input files for this task (NOT ground truth answers)",
                "- candidate_output/: outputs produced by the tested agent (evaluate THIS directory if present)",
                "",
                "Do NOT use any other directories as answers.",
            ]
        )
        + "\n",
    )
    out["readme_path"] = readme_path

    return out


def _build_judge_prompt(
    *,
    task_id: str,
    task_dir: str,
    meta: Dict[str, Json],
    judge_view: Dict[str, str],
) -> str:
    """
    Prompt the ClaudeCode agent to do filesystem-heavy evaluation and emit only JSON.
    """
    rubrics = meta.get("rubrics")
    steps = meta.get("steps")
    task = meta.get("task")
    data = meta.get("data")

    # Keep prompt concise but actionable to reduce token usage (ClaudeCode will inspect by tools/CLI).
    payload = {
        "taskId": task_id,
        "task": task,
        "steps": steps,
        "rubrics": rubrics,
        "taskDir": task_dir,
        "data": data,
        "judgeView": {
            "cwd": judge_view.get("view_dir"),
            "inputsPath": judge_view.get("inputs_visible_path"),
            "originalTaskMetadataPath": judge_view.get("original_task_metadata_path"),
            "candidateOutputPath": judge_view.get("candidate_output_path"),
        },
        "instructions": [
            "你是一个严格的任务评测员（agent-as-a-judge）。",
            "你当前真正可访问的工作目录是 judgeView.cwd，而不是 task JSON 里的系统绝对路径。",
            "为了避免误看 ground truth，judgeView 里只暴露了允许评估的内容：inputs/（原始输入文件）、candidate_output/（待评估输出目录，如果存在）。",
            "禁止把原始任务目录里的 output/output_cc/gt 等目录当成答案来源；本次只允许评估 judgeView.candidateOutputPath 中的结果。",
            "inputs/ 仅用于查看原始输入文件和理解任务，不是标准答案目录。",
            "只能基于你实际检查到的文件/目录/文件内容给出判断，不要凭空假设。",
            "你需要自己决定要检查的具体路径（例如用 ls/find/grep 等），并在 evidence 中写明你检查的路径与观察到的现象。",
            "最终只输出一个 JSON 对象，格式必须为："
            "{ \"rubrics\": [ {\"index\":0,\"passed\":true,\"confidence\":0.8,\"evidence\":\"...\"}, ... ] }",
            "如果证据不足：passed=false，evidence 写清楚缺什么证据。",
        ],
    }
    return (
        "请基于以下输入 JSON 完成 rubrics 评估。\n"
        "注意：最后一行开始请只输出 JSON 对象，不要输出其他文字。\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def evaluate_task(
    task_dir: str,
    *,
    eval_yaml_path: str,
    overwrite: bool = False,
    max_retries: int = 6,
    max_str_len: int = 2000,
    max_trace_items: int = 30,
    max_output_files: int = 10,
) -> Dict[str, Json]:
    """
    I/O-compatible with evaluation_sys/src/agent_eval.py:evaluate_task,
    but uses ClaudeCode.js (agent) to inspect the filesystem and judge rubrics.

    Outputs:
      - rubrics_judge--{model_name}.json
      - dependency_graph--{model_name}.json
    """
    task_dir = os.path.abspath(task_dir)
    eval_yaml_path = os.path.abspath(eval_yaml_path)

    if not os.path.isdir(task_dir):
        return {"error": f"Task directory not found: {task_dir}", "success": False}
    if not os.path.isfile(eval_yaml_path):
        return {"error": f"Eval YAML file not found: {eval_yaml_path}", "success": False}

    if not os.path.exists(os.path.join(task_dir, "output")):
        return {"error": f"Output directory not found: {os.path.join(task_dir, 'output')}", "success": False}

    eval_cfg = _read_yaml(eval_yaml_path)
    base_url = eval_cfg.get("baseUrl")
    model = eval_cfg.get("model")
    api_key = eval_cfg.get("apiKey")
    model_name = eval_cfg.get("model_name") or model or "unknown"

    if not base_url or not model or not api_key:
        return {"error": "Missing baseUrl, model, or apiKey in eval YAML", "success": False}

    task_id = os.path.basename(task_dir)
    kind = _ae._detect_agent_kind(task_dir)
    meta = _ae._load_task_metadata(task_dir)
    if meta is None:
        return {"error": "metadata.json not found or missing rubrics", "success": False, "taskId": task_id}

    rubrics = meta.get("rubrics")
    if not isinstance(rubrics, list) or not rubrics:
        return {"error": "No rubrics found in metadata", "success": False, "taskId": task_id}

    rubrics_out_path = os.path.join(task_dir, f"rubrics_judge--{model_name}.json")
    dep_graph_out_path = os.path.join(task_dir, f"dependency_graph--{model_name}.json")

    result: Dict[str, Json] = {
        "taskId": task_id,
        "taskDir": task_dir,
        "evalModel": model_name,
        "evalYamlPath": eval_yaml_path,
        "success": True,
    }

    if not overwrite and os.path.exists(rubrics_out_path):
        result["rubricsSkipped"] = True
    else:
        sys_prompt = "你是一个严格的任务评测员。"

        # Use a dedicated sandbox under task_dir/raw to keep judge artifacts nearby.
        sandbox_dir = os.path.join(task_dir, "raw", "agent_as_a_judge")
        os.makedirs(sandbox_dir, exist_ok=True)

        api_provider = {
            "provider_type": "anthropic",  # ClaudeCode.js uses anthropic agent sdk; customProvider overrides base/model/key.
            "baseUrl": str(base_url),
            "model": str(model),
            "apiKey": str(api_key),
            "model_name": str(model_name),
        }

        started = time.time()
        tries = 0
        err = ""
        last_text = ""
        usage = None
        rows: List[Json] = []

        while True:
            tries += 1
            sandbox_try_dir = os.path.join(sandbox_dir, f"try_{tries}")
            judge_view = _prepare_judge_view(
                sandbox_try_dir=sandbox_try_dir,
                task_dir=task_dir,
                meta=meta,
            )
            prompt = _build_judge_prompt(
                task_id=task_id,
                task_dir=task_dir,
                meta=meta,
                judge_view=judge_view,
            )
            run_out = _claudecode.run(
                prompt=sys_prompt + "\n\n" + prompt,
                work_dir=judge_view["view_dir"],
                sandbox_dir=sandbox_try_dir,
                timeout_s=600.0,
                api_provider=api_provider,
                agent_id="ClaudeCode.js",
            )

            duration_ms = int((time.time() - started) * 1000)
            tr = run_out.get("trace") if isinstance(run_out, dict) else None
            if isinstance(tr, dict) and isinstance(tr.get("usageTotal"), dict):
                usage = tr.get("usageTotal")
            last_text = tr.get("lastText") if isinstance(tr, dict) and isinstance(tr.get("lastText"), str) else ""

            judged_obj = _json_first_object(last_text)
            if isinstance(judged_obj, dict) and isinstance(judged_obj.get("rubrics"), list):
                for it in judged_obj.get("rubrics"):
                    if not isinstance(it, dict):
                        continue
                    idx = it.get("index")
                    passed = it.get("passed")
                    conf = it.get("confidence")
                    ev = it.get("evidence")
                    if not isinstance(idx, int):
                        continue
                    rows.append(
                        {
                            "index": idx,
                            "rubric": rubrics[idx] if idx < len(rubrics) and isinstance(rubrics[idx], str) else None,
                            "passed": bool(passed) if isinstance(passed, bool) else False,
                            "confidence": float(conf) if isinstance(conf, (int, float)) else None,
                            "evidence": str(ev) if isinstance(ev, str) else "",
                        }
                    )
                err = "" if run_out.get("status") == "ok" else (str(run_out.get("errorMessage") or "")[:2000])
                break

            err = str(run_out.get("errorMessage") or "Judge output parse failed")[:2000]
            if tries >= max_retries:
                break

            # Backoff a bit to reduce rate-limit failures.
            time.sleep(min(60, 2 ** (tries - 1) + random.random()))

        if not rows:
            for i, r in enumerate(rubrics):
                if not isinstance(r, str):
                    continue
                rows.append({"index": i, "rubric": r, "passed": False, "confidence": 0.0, "evidence": f"ClaudeCode judge failed: {err}"})

        passed_n = len([x for x in rows if isinstance(x, dict) and x.get("passed") is True])
        failed_n = len(rows) - passed_n

        _write_json(
            rubrics_out_path,
            {
                "taskId": task_id,
                "agentKind": kind,
                "createdAt": _iso_now(),
                "rubrics": sorted(
                    rows,
                    key=lambda x: int(x.get("index")) if isinstance(x, dict) and isinstance(x.get("index"), int) else 10**9,
                ),
                "summary": {"total": len(rows), "passed": passed_n, "failed": failed_n},
                "judge": {
                    "model": model,
                    "modelName": model_name,
                    "baseUrl": base_url,
                    "usage": usage,
                    "durationMs": int((time.time() - started) * 1000),
                    "tries": tries,
                    "error": err or None,
                    "rawResponseHead": _truncate_str(last_text or "", 2000),
                },
                "prompt": {
                    "system": sys_prompt,
                    "user": _truncate_str(prompt, 4000),
                    "userPromptSizeBytes": len(str(prompt).encode("utf-8")),
                    "userPromptSizeChars": len(str(prompt)),
                },
            },
        )
        result["rubricsPath"] = rubrics_out_path
        result["rubricsSummary"] = {"total": len(rows), "passed": passed_n, "failed": failed_n}

    if not overwrite and os.path.exists(dep_graph_out_path):
        result["depGraphSkipped"] = True
    else:
        dep_graph = _ae._build_dependency_graph(task_dir)
        dep_graph["evalModel"] = model_name
        _write_json(dep_graph_out_path, dep_graph)
        result["depGraphPath"] = dep_graph_out_path
        result["depGraphSummary"] = {"nodes": len(dep_graph.get("nodes", [])), "edges": len(dep_graph.get("edges", []))}

    return result


def evaluate_task_dir(
    task_dir: str,
    *,
    eval_yaml_path: str,
    overwrite: bool = False,
    max_retries: int = 6,
    max_str_len: int = 2000,
    max_trace_items: int = 30,
    max_output_files: int = 10,
) -> Dict[str, Json]:
    """Alias for compatibility (same as agent_eval.py)."""
    return evaluate_task(
        task_dir,
        eval_yaml_path=eval_yaml_path,
        overwrite=overwrite,
        max_retries=max_retries,
        max_str_len=max_str_len,
        max_trace_items=max_trace_items,
        max_output_files=max_output_files,
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Evaluate task(s) using ClaudeCode agent-as-a-judge")
    p.add_argument("--task-dir", required=True, help="Path to task execution result directory or runs root")
    p.add_argument("--eval-yaml", required=True, help="Path to eval YAML (baseUrl/model/apiKey/model_name)")
    p.add_argument("--overwrite", action="store_true", help="Overwrite existing evaluation results")
    p.add_argument("--parallel", action="store_true", help="Enable parallel evaluation across tasks")
    p.add_argument("--workers", type=int, default=5)
    p.add_argument("--max-retries", type=int, default=6)
    p.add_argument("--max-str-len", type=int, default=2000)
    p.add_argument("--max-trace-items", type=int, default=30)
    p.add_argument("--max-output-files", type=int, default=10)
    args = p.parse_args()

    # If args.task_dir is a runs root, evaluate each subdir containing metadata.json
    task_dirs: List[str] = []
    if os.path.isdir(args.task_dir):
        for entry in os.listdir(args.task_dir):
            entry_path = os.path.join(args.task_dir, entry)
            if os.path.isdir(entry_path) and os.path.isfile(os.path.join(entry_path, "metadata.json")):
                task_dirs.append(entry_path)

    if not task_dirs:
        task_dirs = [args.task_dir]

    if args.parallel and len(task_dirs) > 1:
        max_workers = min(args.workers, len(task_dirs))
        with tqdm(total=len(task_dirs), desc="Evaluating tasks") as pbar:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(
                        evaluate_task,
                        task_dir=td,
                        eval_yaml_path=args.eval_yaml,
                        overwrite=args.overwrite,
                        max_retries=args.max_retries,
                        max_str_len=args.max_str_len,
                        max_trace_items=args.max_trace_items,
                        max_output_files=args.max_output_files,
                    ): td
                    for td in task_dirs
                }
                for fut in as_completed(futures):
                    _ = fut.result()
                    pbar.update(1)
                    # print(json.dumps(_, ensure_ascii=False, indent=2))
    else:
        for td in tqdm(task_dirs, desc="Evaluating tasks"):
            _ = evaluate_task(
                task_dir=td,
                eval_yaml_path=args.eval_yaml,
                overwrite=args.overwrite,
                max_retries=args.max_retries,
                max_str_len=args.max_str_len,
                max_trace_items=args.max_trace_items,
                max_output_files=args.max_output_files,
            )
            # print(json.dumps(_, ensure_ascii=False, indent=2))

"""
python3 src/agent_as_a_judge.py \
    --task-dir /path/to/Workspace-Bench/evaluation/output/Codex--Kimi-K2.5--Lite \
    --eval-yaml /path/to/Workspace-Bench/evaluation/runs/judge.yaml \
    --parallel \
    --workers 3
"""
