from posixpath import basename
from tqdm import tqdm
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import importlib.util
import json
import os
import re
import shutil
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

import yaml
import sys
from pathlib import Path

from filesys_utils import filesys_rollback
import agent_as_a_judge

Json = Any

def _repo_root() -> Path:
    return Path(__file__).resolve().parent

def _ensure_import_path() -> None:
    root = str(_repo_root())
    if root not in sys.path:
        sys.path.insert(0, root)

def _ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)


def _read_json(path: str) -> Json:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: str, obj: Json) -> None:
    _ensure_dir(os.path.dirname(os.path.abspath(path)))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _read_yaml(path: str) -> Dict[str, Json]:
    with open(path, "r", encoding="utf-8") as f:
        obj = yaml.safe_load(f)
    if not isinstance(obj, dict):
        raise ValueError("run yaml must be a mapping")
    return _expand_config_env(obj)


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


def _inputs_dir_from_meta(meta: Dict[str, Json]) -> str:
    mp = meta.get("__metadata_path")
    if isinstance(mp, str) and mp.strip():
        base = os.path.dirname(os.path.abspath(mp))
        cand = os.path.join(base, "data")
        if os.path.isdir(cand):
            return cand
    return ""


def _with_env(overrides: Dict[str, str]):
    """
    Context manager-like helper without importing contextlib.
    """
    old: Dict[str, Optional[str]] = {}
    for k, v in overrides.items():
        old[k] = os.environ.get(k)
        os.environ[k] = v

    def _restore():
        for k, prev in old.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev

    return _restore


def _safe_name(s: str) -> str:
    out = []
    for ch in str(s or ""):
        if ch.isalnum() or ch in {"-", "_", "."}:
            out.append(ch)
        else:
            out.append("_")
    return ("".join(out)[:120] or "item")


def _normalize_rel_path(p: str) -> str:
    s = str(p or "").strip().replace("\\", "/")
    while s.startswith("/"):
        s = s[1:]
    return s


def _iter_metadata_paths(root: str, *, limit: Optional[int] = None) -> List[str]:
    root = os.path.abspath(root)
    if os.path.isfile(root) and os.path.basename(root) == "metadata.json":
        return [root]
    if os.path.isfile(root):
        return []
    out: List[str] = []
    lim = None if limit is None else max(0, int(limit))
    try:
        entries = sorted(os.listdir(root))
    except Exception:
        return out
    for name in entries:
        task_dir = os.path.join(root, name)
        if not os.path.isdir(task_dir):
            continue
        meta_path = os.path.join(task_dir, "metadata.json")
        if os.path.isfile(meta_path):
            out.append(meta_path)
            if lim is not None and len(out) >= lim:
                return out
    if out:
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        if "metadata.json" in filenames:
            out.append(os.path.join(dirpath, "metadata.json"))
            if lim is not None and len(out) >= lim:
                break
    return out


def _load_metadatas(tasks_root: str, *, limit: Optional[int]) -> List[Dict[str, Json]]:
    metas: List[Dict[str, Json]] = []
    for mp in _iter_metadata_paths(tasks_root, limit=limit):
        try:
            meta = _read_json(mp)
        except Exception:
            continue
        if not isinstance(meta, dict):
            continue
        m = dict(meta)
        m["__metadata_path"] = mp
        metas.append(m)
    return metas


def _resolve_work_dir(meta: Dict[str, Json], fs_map: Dict[str, str]) -> str:
    fs = meta.get("file_system")
    if isinstance(fs, str) and fs in fs_map:
        return str(fs_map.get(fs))
    return str(fs_map.get("*") or "")


def _copy_from_manifest(meta: Dict[str, Json], *, work_dir: str) -> List[str]:
    created: List[str] = []
    mp = meta.get("__metadata_path")
    source_base = os.path.dirname(os.path.abspath(str(mp))) if isinstance(mp, str) and mp else None
    if not source_base:
        return created
    dm = meta.get("data_manifest")
    if not isinstance(dm, list):
        return created
    for it in dm:
        if not isinstance(it, dict):
            continue
        target_path = it.get("target_path")
        stored_relpath = it.get("stored_relpath")
        if not isinstance(target_path, str) or not isinstance(stored_relpath, str):
            continue
        src = os.path.abspath(os.path.join(source_base, stored_relpath))
        if not os.path.isfile(src):
            continue
        rel_target = _normalize_rel_path(target_path)
        dst = os.path.abspath(os.path.join(work_dir, rel_target))
        _ensure_dir(os.path.dirname(dst))
        shutil.copy2(src, dst)
        created.append(dst)
    return created


def _expected_output_files(meta: Dict[str, Json]) -> List[str]:
    output_files = meta.get("output_files")
    if isinstance(output_files, list):
        out = [os.path.basename(str(x)).strip() for x in output_files if str(x).strip()]
        if out:
            return out

    of = meta.get("output_file")
    if isinstance(of, str) and of.strip():
        return [of.strip()]

    ofs = meta.get("output_manifests")
    if isinstance(ofs, list):
        out = [os.path.basename(x.get("stored_relpath", "")).strip()[os.path.basename(x.get("stored_relpath", "")).find("_") + 1:] for x in ofs if isinstance(x, dict) and x.get("stored_relpath")]
        if out:
            return out
    return []


def _wrap_prompt(*, prompt: str, work_dir: str, prompt_head: str, prompt_tail: str, task_target_output_dir: str) -> str:
    if task_target_output_dir != "":
        path_requirement = f"请你无视任务要求中的输出文件保存路径要求，将所有输出文件放置在目录：{os.path.join(work_dir, task_target_output_dir)}下\n"
    else:
        path_requirement = ""
    head = (
        "【重要要求 1：工作目录】\n"
        f"本轮测试允许访问的工作目录是：{os.path.abspath(work_dir)}\n"
        "你只能在该目录下使用相对路径读写文件；禁止访问工作目录以外的位置。\n"
        "如果你看到其他工作区路径提示，请忽略，以本提示的工作目录为准。\n"
        f"{path_requirement}"
    )
    tail = (
        "\n【重要要求 2：输出路径列表】\n"
        "在最后一步，请仅输出一个 Python 列表（list[str]），里面是你生成的所有输出文件路径。\n"
        "路径请使用相对工作目录的相对路径（不要以 / 开头）。示例：['output/a.txt','report.md']\n"
    )
    p = str(prompt or "").strip()
    p2 = (str(prompt_head or "") + ("\n" if prompt_head else "") + p + ("\n" if prompt_tail else "") + str(prompt_tail or "")).strip()
    return head + "\n" + p2 + "\n" + tail


def _parse_python_list_paths(text: str) -> List[str]:
    import ast

    s = str(text or "").strip()
    if not s:
        return []
    try:
        obj = ast.literal_eval(s)
    except Exception:
        return []
    if not isinstance(obj, list):
        return []
    out: List[str] = []
    for x in obj:
        if isinstance(x, str) and x.strip() and not x.strip().startswith("/"):
            out.append(x.strip())
    return out


def _find_by_basename(root: str, basenames: List[str]) -> List[str]:
    want = set([os.path.basename(b) for b in basenames if isinstance(b, str) and b])
    if not want:
        return []
    found: List[str] = []
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            if fn in want:
                found.append(os.path.abspath(os.path.join(dirpath, fn)))
    return sorted(set(found))

def _find_by_fullname(root: str, fullnames: List[str]) -> List[str]:
    want = set([b for b in fullnames if isinstance(b, str) and b])
    if not want:
        return []
    found: List[str] = []
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            if fn in want:
                found.append(os.path.abspath(os.path.join(dirpath, fn)))
    return sorted(set(found))


def _pick_recent_by_basename(paths: List[str], *, min_mtime: Optional[float]) -> List[str]:
    if not paths:
        return []
    by_name: Dict[str, Tuple[float, str]] = {}
    for p in paths:
        if not p or not os.path.isfile(p):
            continue
        try:
            mt = os.path.getmtime(p)
        except Exception:
            continue
        if min_mtime is not None and mt < float(min_mtime):
            continue
        bn = os.path.basename(p)
        cur = by_name.get(bn)
        if cur is None or mt > cur[0]:
            by_name[bn] = (mt, p)
    out = [v[1] for v in by_name.values()]
    return sorted(set(out))


def _resolve_under(root: str, p: str) -> str:
    rel = _normalize_rel_path(p)
    abs_p = os.path.abspath(os.path.join(root, rel))
    root_abs = os.path.abspath(root)
    if abs_p != root_abs and not abs_p.startswith(root_abs + os.sep):
        raise ValueError("path escapes work dir")
    return abs_p


def _collect_output_paths(
    *,
    task_target_output_dir: str,
    work_dir: str,
    expected_files: List[str],
    returned_paths: List[str],
    last_text: str,
    min_mtime: Optional[float],
) -> Tuple[List[str], str]:
    out: List[str] = []
    retrieval_method = []
    for rp in _parse_python_list_paths(last_text):
        try:
            ap = _resolve_under(work_dir, rp)
        except Exception:
            continue
        if os.path.isfile(ap):
            out.append(ap)
    if out:
        retrieval_method.append("last_text_paths")
        # return (sorted(set(out)), "last_text_paths")

    found = _find_by_fullname(work_dir, expected_files)
    # picked = _pick_recent_by_basename(found, min_mtime=min_mtime)
    if found:
        retrieval_method.append("expected_filenames_recent")
        out.extend(found)
        # return (sorted(set(found)), "expected_filenames_recent")

    found2 = []
    for p in returned_paths:
        if not isinstance(p, str) or not p:
            continue
        try:
            ap = _resolve_under(work_dir, p)
        except Exception:
            continue
        if os.path.isfile(ap):
            found2.append(ap)
    # picked2 = _pick_recent_by_basename(out, min_mtime=min_mtime)
    if found2:
        retrieval_method.append("returned_paths_recent")
        out.extend(found2)
        # return (sorted(set(found2)), "returned_paths_recent")
    
    if task_target_output_dir != "":
        found3 = []
        # 获取task_target_output_dir目录下所有文件的路径
        for root, dirs, files in os.walk(os.path.join(work_dir, task_target_output_dir)):
            # print(files)
            for file in files:
                file_path = os.path.abspath(os.path.join(root, file))
                found3.append(file_path)
        if found3:
            retrieval_method.append("task_target_output_dir")
            out.extend(found3)

    # print(sorted(set(out)))
    return (sorted(set(out)), retrieval_method)

def _read_text_limited(path: str, *, limit: int = 200000) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            s = f.read(limit + 1)
        if len(s) > limit:
            return s[:limit]
        return s
    except Exception:
        return None


def _copy_outputs(*, output_paths: List[str], out_dir: str) -> List[Dict[str, Json]]:
    _ensure_dir(out_dir)
    manifest: List[Dict[str, Json]] = []
    for src in output_paths:
        if not src or not os.path.isfile(src):
            continue
        base = os.path.basename(src) or "output"
        dst = os.path.join(out_dir, base)
        if os.path.abspath(dst) == os.path.abspath(src):
            continue
        if os.path.exists(dst):
            i = 1
            b, ext = os.path.splitext(base)
            while os.path.exists(dst):
                dst = os.path.join(out_dir, f"{b}_{i}{ext}")
                i += 1
        shutil.copy2(src, dst)
        manifest.append(
            {"sourcePath": os.path.basename(src), "outputPath": os.path.relpath(dst, out_dir), "sizeBytes": os.path.getsize(dst)}
        )
    return manifest


def _load_agent_run(agent_name: str):
    here = os.path.abspath(os.path.dirname(__file__))
    agent_path = os.path.join(here, "agents", f"{agent_name.lower()}.py")
    if not os.path.exists(agent_path):
        raise FileNotFoundError(f"missing agent file: {agent_path}")
    spec = importlib.util.spec_from_file_location(f"evaluation_sys.agents.{agent_name.lower()}", agent_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load agent module")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn = getattr(mod, "run", None)
    if not callable(fn):
        raise RuntimeError("agent module missing run()")
    return fn


def _new_summary() -> Dict[str, int]:
    return {"total": 0, "passed": 0, "failed": 0, "error": 0, "timeout": 0}


def _merge_summary(dst: Dict[str, int], src: Dict[str, int]) -> None:
    for key in ("total", "passed", "failed", "error", "timeout"):
        dst[key] = int(dst.get(key, 0)) + int(src.get(key, 0))


def _as_bool(value: Json, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off"}:
            return False
    return default


def _group_metas_by_file_system(metas: List[Dict[str, Json]]) -> Dict[str, List[Tuple[int, Dict[str, Json]]]]:
    grouped: Dict[str, List[Tuple[int, Dict[str, Json]]]] = {}
    for idx, meta in enumerate(metas):
        fs_name = str(meta.get("file_system") or "*")
        grouped.setdefault(fs_name, []).append((idx, meta))
    return grouped


def _run_one_case(
    *,
    idx: int,
    meta: Dict[str, Json],
    runs_root: str,
    run_fn,
    prompt_head: str,
    prompt_tail: str,
    task_target_output_dir: str,
    timeout_sec: float,
    api_provider: Dict[str, Json],
    eval_while_running: bool,
    eval_yaml: str,
    work_dir_map: Dict[str, str],
    standard_work_dir_map: Dict[str, str],
    agent_name: str,
    model_name: str,
) -> Dict[str, Json]:
    print(f"run task: {meta.get('id') or ''}")
    summary = _new_summary()
    summary["total"] = 1

    case_id = str(meta.get("id") or os.path.basename(os.path.dirname(str(meta.get("__metadata_path") or ""))) or "case_001")
    case_id_safe = _safe_name(case_id)
    case_dir = os.path.join(runs_root, case_id_safe)

    if os.path.exists(case_dir) and os.path.exists(os.path.join(case_dir, "output")) and os.listdir(os.path.join(case_dir, "output")):
        existing_agent = _read_json(os.path.join(case_dir, "agent.json"))
        status_existing = "passed"
        duration_existing = None
        output_files_existing: List[Dict[str, Json]] = []
        if isinstance(existing_agent, dict):
            status_existing = str(existing_agent.get("status") or "passed")
            duration_existing = existing_agent.get("durationMs") if isinstance(existing_agent.get("durationMs"), int) else None
            trace = existing_agent.get("trace") if isinstance(existing_agent.get("trace"), dict) else {}
            outputs = trace.get("outputs") if isinstance(trace.get("outputs"), dict) else {}
            manifest = outputs.get("outputManifest") if isinstance(outputs.get("outputManifest"), list) else []
            output_files_existing = [x for x in manifest if isinstance(x, dict)]
        if status_existing not in {"passed", "failed", "error", "timeout"}:
            status_existing = "passed"
        if not output_files_existing:
            out_dir = os.path.join(case_dir, "output")
            for root, _, files in os.walk(out_dir):
                for name in files:
                    path = os.path.join(root, name)
                    rel = os.path.relpath(path, out_dir).replace("\\", "/")
                    output_files_existing.append({"sourcePath": rel, "outputPath": rel, "sizeBytes": os.path.getsize(path)})
        summary[status_existing] += 1
        return {
            "index": idx,
            "summary": summary,
            "case": {
                "caseId": case_id,
                "outputDir": case_dir,
                "status": status_existing,
                "durationMs": duration_existing,
                "outputFiles": output_files_existing,
                "resumed": True,
            },
        }
    elif os.path.exists(case_dir):
        shutil.rmtree(case_dir)
    _ensure_dir(case_dir)

    _write_json(os.path.join(case_dir, "metadata.json"), meta)

    work_dir = _resolve_work_dir(meta, {str(k): str(v) for k, v in work_dir_map.items()})
    if not work_dir:
        raise RuntimeError("cannot resolve work dir")
    _ensure_dir(work_dir)

    _copy_from_manifest(meta, work_dir=work_dir)

    prompt = _wrap_prompt(
        prompt=str(meta.get("task") or ""),
        work_dir=work_dir,
        prompt_head=prompt_head,
        prompt_tail=prompt_tail,
        task_target_output_dir=task_target_output_dir,
    )
    expected_files = _expected_output_files(meta)

    raw_dir = os.path.join(case_dir, "raw")
    _ensure_dir(raw_dir)

    case_started = time.time()
    api_provider2 = dict(api_provider) if isinstance(api_provider, dict) else {}
    api_provider2["__expected_output_files__"] = expected_files
    run_res = run_fn(
        prompt=prompt,
        work_dir=work_dir,
        sandbox_dir=case_dir,
        timeout_s=timeout_sec,
        api_provider=api_provider2,
    )
    duration_ms = int((time.time() - case_started) * 1000)

    status_raw = str(run_res.get("status") or "").strip().lower()
    if status_raw not in {"ok", "timeout", "error"}:
        status_raw = "error"

    returned_paths_abs = run_res.get("paths") if isinstance(run_res.get("paths"), list) else []
    returned_paths_rel: List[str] = []
    for apath in returned_paths_abs:
        if not isinstance(apath, str) or not apath:
            continue
        try:
            rel = os.path.relpath(os.path.abspath(apath), os.path.abspath(work_dir))
        except Exception:
            continue
        if rel.startswith(".."):
            continue
        returned_paths_rel.append(rel.replace("\\", "/"))

    trace_obj = run_res.get("trace") if isinstance(run_res.get("trace"), dict) else {}
    last_text = str(trace_obj.get("lastText")) if isinstance(trace_obj.get("lastText"), str) else ""

    if status_raw != "ok":
        output_paths, retrieval_method = ([], "skipped")
        manifest = []
    else:
        output_paths, retrieval_method = _collect_output_paths(
            work_dir=work_dir,
            expected_files=expected_files,
            task_target_output_dir=task_target_output_dir,
            returned_paths=returned_paths_rel,
            last_text=last_text,
            min_mtime=case_started - 1.0,
        )
        manifest = _copy_outputs(output_paths=output_paths, out_dir=os.path.join(case_dir, "output"))

    checks = []
    if status_raw != "ok":
        checks.append({"type": "returned_paths_exist", "passed": False, "detail": f"skipped_due_to_status:{status_raw}"})
    elif output_paths:
        checks.append({"type": "returned_paths_exist", "passed": True, "detail": {"count": len(output_paths)}})
    else:
        checks.append({"type": "returned_paths_exist", "passed": False, "detail": "Agent returned empty path list"})

    if status_raw == "timeout":
        final_status = "timeout"
        summary["timeout"] += 1
    elif status_raw == "error":
        final_status = "error"
        summary["error"] += 1
    else:
        final_status = "passed" if output_paths else "failed"
        summary["passed" if output_paths else "failed"] += 1

    metrics_obj = run_res.get("metrics") if isinstance(run_res.get("metrics"), dict) else {}
    turns = metrics_obj.get("turns") if isinstance(metrics_obj.get("turns"), int) else None
    prompt_tokens = metrics_obj.get("promptTokens") if isinstance(metrics_obj.get("promptTokens"), int) else None
    completion_tokens = metrics_obj.get("completionTokens") if isinstance(metrics_obj.get("completionTokens"), int) else None
    total_tokens = metrics_obj.get("totalTokens") if isinstance(metrics_obj.get("totalTokens"), int) else None

    stdout_txt = _read_text_limited(os.path.join(raw_dir, "stdout.txt"))
    stderr_txt = _read_text_limited(os.path.join(raw_dir, "stderr.txt"))

    exec_from_agent = trace_obj.get("executionTrace") if isinstance(trace_obj.get("executionTrace"), list) else None
    llm_from_agent = trace_obj.get("llm") if isinstance(trace_obj.get("llm"), dict) else None
    usage_total_from_agent = trace_obj.get("usageTotal") if isinstance(trace_obj.get("usageTotal"), dict) else None

    with open(os.path.join(case_dir, "agent.log"), "w", encoding="utf-8") as f:
        f.write(f"agent={agent_name} model={model_name}\n")
        f.write(f"workDir={os.path.abspath(work_dir)}\n")
        f.write(f"baseUrl={api_provider.get('baseUrl') if isinstance(api_provider, dict) else ''}\n")
        f.write(f"llmModel={api_provider.get('model') if isinstance(api_provider, dict) else ''}\n")
        f.write(f"timeoutSec={timeout_sec}\n")
        f.write(f"status={final_status} durationMs={duration_ms}\n")
        f.write(f"turns={turns} promptTokens={prompt_tokens} completionTokens={completion_tokens} totalTokens={total_tokens}\n")
        f.write(f"retrievalMethod={retrieval_method} outputs={len(output_paths)} returnedPaths={len(returned_paths_rel)}\n")
        if isinstance(exec_from_agent, list) and exec_from_agent:
            f.write(f"executionTrace={len(exec_from_agent)}\n")

    agent_json = {
        "caseId": case_id,
        "name": str(meta.get("id_prefix") or meta.get("name") or ""),
        "workDir": os.path.abspath(work_dir),
        "status": final_status,
        "durationMs": duration_ms,
        "turns": turns,
        "promptTokens": prompt_tokens,
        "completionTokens": completion_tokens,
        "totalTokens": total_tokens,
        "checks": checks,
        "errorType": ("Timeout" if final_status == "timeout" else ("RunnerError" if final_status == "error" else None)),
        "errorMessage": run_res.get("errorMessage") if isinstance(run_res.get("errorMessage"), str) else None,
        "traceback": None,
        "trace": {
            "prompt": {"system": None, "user": prompt, "promptTail": prompt_tail or None},
            "executionTrace": exec_from_agent or [],
            "llm": {
                "provider": (llm_from_agent.get("provider") if isinstance(llm_from_agent, dict) else (str(api_provider.get("provider_type") or "") if isinstance(api_provider, dict) else None)),
                "baseUrl": (llm_from_agent.get("baseUrl") if isinstance(llm_from_agent, dict) else (api_provider.get("baseUrl") if isinstance(api_provider, dict) else None)),
                "model": (llm_from_agent.get("model") if isinstance(llm_from_agent, dict) else (api_provider.get("model") if isinstance(api_provider, dict) else None)),
                "usageTotal": usage_total_from_agent or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            },
            "outputs": {
                "returnedPaths": returned_paths_rel,
                "retrievalMethod": retrieval_method,
                "outputManifest": manifest,
            },
            "raw": {"stdout": stdout_txt, "stderr": stderr_txt},
        },
    }
    if isinstance(trace_obj, dict):
        agent_json["trace"]["raw"]["runner"] = {
            k: v
            for k, v in trace_obj.items()
            if k not in {"apiKey", "api_key", "openaiApiKey", "arkApiKey", "token", "auth", "authorization"}
        }

    _write_json(os.path.join(case_dir, "agent.json"), agent_json)
    case_out = {
        "caseId": case_id,
        "outputDir": case_dir,
        "status": final_status,
        "durationMs": duration_ms,
        "outputFiles": manifest,
    }

    if eval_while_running:
        try:
            judge_res = agent_as_a_judge.evaluate_task(
                task_dir=case_dir,
                eval_yaml_path=eval_yaml,
                overwrite=True,
                max_retries=3,
            )
            if not (isinstance(judge_res, dict) and judge_res.get("success") is True):
                judge_err = None
                if isinstance(judge_res, dict):
                    judge_err = judge_res.get("error") or judge_res.get("message")
                with open(os.path.join(case_dir, "agent.log"), "a", encoding="utf-8") as f:
                    f.write(f"\njudge_error=JudgeFailed: {judge_err or 'unknown error'}\n")
        except Exception as e:
            with open(os.path.join(case_dir, "agent.log"), "a", encoding="utf-8") as f:
                f.write(f"\njudge_error={type(e).__name__}: {e}\n")

    try:
        filesys_rollback(
            standard_work_dir=standard_work_dir_map[meta["file_system"]],
            work_dir=work_dir_map[meta["file_system"]],
        )
    except Exception as e:
        with open(os.path.join(case_dir, "agent.log"), "a", encoding="utf-8") as f:
            f.write(f"\nrollback_error={type(e).__name__}: {e}\n")

    return {"index": idx, "summary": summary, "case": case_out}


def _run_group(
    *,
    group_items: List[Tuple[int, Dict[str, Json]]],
    runs_root: str,
    run_fn,
    prompt_head: str,
    prompt_tail: str,
    task_target_output_dir: str,
    timeout_sec: float,
    api_provider: Dict[str, Json],
    eval_while_running: bool,
    eval_yaml: str,
    work_dir_map: Dict[str, str],
    standard_work_dir_map: Dict[str, str],
    agent_name: str,
    model_name: str,
) -> Dict[str, Json]:
    group_summary = _new_summary()
    group_cases: List[Tuple[int, Dict[str, Json]]] = []
    for idx, meta in group_items:
        res = _run_one_case(
            idx=idx,
            meta=meta,
            runs_root=runs_root,
            run_fn=run_fn,
            prompt_head=prompt_head,
            prompt_tail=prompt_tail,
            task_target_output_dir=task_target_output_dir,
            timeout_sec=timeout_sec,
            api_provider=api_provider,
            eval_while_running=eval_while_running,
            eval_yaml=eval_yaml,
            work_dir_map=work_dir_map,
            standard_work_dir_map=standard_work_dir_map,
            agent_name=agent_name,
            model_name=model_name,
        )
        _merge_summary(group_summary, res["summary"])
        if isinstance(res.get("case"), dict):
            group_cases.append((int(res["index"]), res["case"]))
    return {"summary": group_summary, "cases": group_cases, "processed": len(group_items)}


def main() -> None:
    _ensure_import_path()

    ap = argparse.ArgumentParser()
    ap.add_argument("--run-config", required=True)
    args = ap.parse_args()

    workspace_root = str(Path(__file__).resolve().parents[2])
    eval_root = str(Path(__file__).resolve().parents[1])
    os.environ.setdefault("WORKSPACE_BENCH_ROOT", os.environ.get("RIP_BENCH_ROOT", workspace_root))
    os.environ.setdefault("WORKSPACE_BENCH_EVAL_ROOT", os.environ.get("RIP_BENCH_EVAL_ROOT", eval_root))
    os.environ.setdefault("RIP_BENCH_ROOT", os.environ["WORKSPACE_BENCH_ROOT"])
    os.environ.setdefault("RIP_BENCH_EVAL_ROOT", os.environ["WORKSPACE_BENCH_EVAL_ROOT"])

    cfg = _read_yaml(args.run_config)
    agent_name = str(cfg.get("agent_name") or "").strip()
    model_name = str(cfg.get("model_name") or "").strip()
    run_name = str(cfg.get("run_name") or "").strip()
    task_path = str(cfg.get("task_path") or "").strip()
    task_target_output_dir = str(cfg.get("task_target_output_dir") or "").strip()
    output_dir = str(cfg.get("output_dir") or "").strip()
    fs_map_file = str(cfg.get("fs_map_file") or "").strip()
    prompt_head = str(cfg.get("prompt_head") or "")
    prompt_tail = str(cfg.get("prompt_tail") or "")
    task_limit = cfg.get("task_limit")
    timeout_sec = float(cfg.get("timeout_sec") or 300.0)
    api_provider = cfg.get("api_provider") if isinstance(cfg.get("api_provider"), dict) else {}

    eval_while_running = cfg.get("eval_while_running") or False
    eval_yaml = str(cfg.get("eval_yaml") or "").strip()
    workdir_parallel = _as_bool(cfg.get("workdir_parallel"), default=True)

    assert agent_name and model_name and run_name and task_path and output_dir and fs_map_file

    runs_root = os.path.abspath(os.path.join(output_dir, f"{agent_name}--{model_name}--{run_name}"))
    _ensure_dir(runs_root)

    fs_map_all = _read_json(os.path.abspath(fs_map_file))
    assert isinstance(fs_map_all, dict) and fs_map_all

    work_dir_map = fs_map_all.get("work_dir", {})
    raw_work_dir_map = fs_map_all.get("raw_work_dir", {})
    standard_work_dir_map = fs_map_all.get("standard_work_dir", {})

    metas = _load_metadatas(task_path, limit=int(task_limit) if task_limit is not None else None)
    if not metas:
        raise SystemExit("no tasks found")

    run_fn = _load_agent_run(agent_name)

    summary = _new_summary()
    cases_by_index: Dict[int, Dict[str, Json]] = {}
    started = time.time()
    if workdir_parallel:
        grouped = _group_metas_by_file_system(metas)
    else:
        grouped = {"__all__": list(enumerate(metas))}

    with tqdm(total=len(metas), desc=f"{agent_name}--{model_name}--{run_name}") as pbar:
        if workdir_parallel:
            max_workers = min(5, max(1, len(grouped)))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_group: Dict[Any, Dict[str, Json]] = {}
                for group_name, group_items in grouped.items():
                    fut = executor.submit(
                        _run_group,
                        group_items=group_items,
                        runs_root=runs_root,
                        run_fn=run_fn,
                        prompt_head=prompt_head,
                        prompt_tail=prompt_tail,
                        task_target_output_dir=task_target_output_dir,
                        timeout_sec=timeout_sec,
                        api_provider=api_provider,
                        eval_while_running=eval_while_running,
                        eval_yaml=eval_yaml,
                        work_dir_map=work_dir_map,
                        standard_work_dir_map=standard_work_dir_map,
                        agent_name=agent_name,
                        model_name=model_name,
                    )
                    future_to_group[fut] = {
                        "groupName": group_name,
                        "caseIds": [str(meta.get("id") or "") for _, meta in group_items],
                    }
                for fut in as_completed(future_to_group):
                    group_info = future_to_group[fut]
                    try:
                        res = fut.result()
                    except Exception:
                        print(
                            "[parallel-error] "
                            f"group={group_info.get('groupName')} "
                            f"cases={group_info.get('caseIds')}",
                            flush=True,
                        )
                        print(traceback.format_exc(), flush=True)
                        raise
                    _merge_summary(summary, res["summary"])
                    for idx, case_out in res["cases"]:
                        cases_by_index[int(idx)] = case_out
                    pbar.update(int(res.get("processed", 0)))
        else:
            for _, group_items in grouped.items():
                res = _run_group(
                    group_items=group_items,
                    runs_root=runs_root,
                    run_fn=run_fn,
                    prompt_head=prompt_head,
                    prompt_tail=prompt_tail,
                    task_target_output_dir=task_target_output_dir,
                    timeout_sec=timeout_sec,
                    api_provider=api_provider,
                    eval_while_running=eval_while_running,
                    eval_yaml=eval_yaml,
                    work_dir_map=work_dir_map,
                    standard_work_dir_map=standard_work_dir_map,
                    agent_name=agent_name,
                    model_name=model_name,
                )
                _merge_summary(summary, res["summary"])
                for idx, case_out in res["cases"]:
                    cases_by_index[int(idx)] = case_out
                pbar.update(int(res.get("processed", 0)))

    cases_out = [cases_by_index[idx] for idx in sorted(cases_by_index.keys())]

    finished = time.time()
    cfg2 = json.loads(json.dumps(cfg, ensure_ascii=False, default=str))
    if isinstance(cfg2, dict) and isinstance(cfg2.get("api_provider"), dict):
        cfg2["api_provider"].pop("apiKey", None)
    report = {
        "runsRoot": runs_root,
        "agentId": agent_name,
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "finishedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(finished)),
        "totalDurationMs": int((finished - started) * 1000),
        "summary": summary,
        "cases": cases_out,
        "config": cfg2,
    }
    _write_json(os.path.join(runs_root, "agent_runner_report.json"), report)


if __name__ == "__main__":
    main()
