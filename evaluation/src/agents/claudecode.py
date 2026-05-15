import json
import os
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

Json = Any

BASELINES_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "baselines"))
CLAUDECODE_JS = os.path.join(BASELINES_DIR, "ClaudeCode.js")


def _ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)


def _write_json(path: str, obj: Json) -> None:
    _ensure_dir(os.path.dirname(os.path.abspath(path)))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _read_json(path: str) -> Json:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _safe_json_loads(line: str) -> Optional[Dict[str, Json]]:
    try:
        obj = json.loads(line)
    except Exception:
        return None
    if isinstance(obj, dict):
        return obj
    return None


def _ms_to_iso(ms: Optional[int]) -> Optional[str]:
    if not isinstance(ms, int):
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms / 1000.0)) + "Z"
    except Exception:
        return None


def _parse_usage_from_stdout(stdout_text: str) -> Tuple[Dict[str, int], Optional[str], Optional[str]]:
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cache_read": 0, "cache_write": 0}
    provider = None
    model = None
    for raw in str(stdout_text or "").splitlines():
        evt = _safe_json_loads(raw.strip())
        if not isinstance(evt, dict):
            continue
        if evt.get("type") != "result":
            continue
        part = evt.get("part")
        if isinstance(part, dict):
            if isinstance(part.get("provider"), str):
                provider = part.get("provider")
            if isinstance(part.get("model"), str):
                model = part.get("model")
        u = evt.get("usage")
        if not isinstance(u, dict):
            u = part.get("usage") if isinstance(part, dict) else None
        if not isinstance(u, dict):
            continue
        pt = u.get("input_tokens") if isinstance(u.get("input_tokens"), int) else u.get("prompt_tokens")
        ct = u.get("output_tokens") if isinstance(u.get("output_tokens"), int) else u.get("completion_tokens")
        if isinstance(pt, int):
            usage_total["prompt_tokens"] += pt
        if isinstance(ct, int):
            usage_total["completion_tokens"] += ct
    usage_total["total_tokens"] = usage_total["total_tokens"] or (usage_total["prompt_tokens"] + usage_total["completion_tokens"])
    return usage_total, provider, model


def _build_execution_trace(*, prompt: str, started_ms: int, task_result: Dict[str, Json], usage_total: Dict[str, int], llm_base_url: Optional[str], llm_model: Optional[str], llm_provider: Optional[str]) -> List[Dict[str, Json]]:
    out: List[Dict[str, Json]] = []
    out.append(
        {
            "type": "text",
            "role": "user",
            "content": str(prompt or ""),
            "timestamp": _ms_to_iso(started_ms),
        }
    )

    traj = task_result.get("trajectory")
    if isinstance(traj, list):
        for it in traj:
            if not isinstance(it, dict):
                continue
            typ = it.get("type")
            ts_ms = it.get("timestamp") if isinstance(it.get("timestamp"), int) else None
            ts = _ms_to_iso(ts_ms)
            if typ == "text":
                txt = it.get("text") if isinstance(it.get("text"), str) else ""
                ev: Dict[str, Json] = {
                    "type": "text",
                    "role": "assistant",
                    "content": txt,
                    "timestamp": ts,
                    "turn": None,
                    "llm": {
                        "provider": llm_provider,
                        "baseUrl": llm_base_url,
                        "model": llm_model,
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cache_read": 0, "cache_write": 0},
                        "stopReason": None,
                        "errorMessage": None,
                    },
                }
                out.append(ev)
            elif typ == "tool_call":
                tool = it.get("tool") if isinstance(it.get("tool"), str) else None
                call_id = it.get("callID") if isinstance(it.get("callID"), str) else None
                dur = it.get("durationMs") if isinstance(it.get("durationMs"), int) else None
                exit_code = it.get("exitCode") if isinstance(it.get("exitCode"), int) else None
                state = it.get("state") if isinstance(it.get("state"), str) else None
                status = None
                if state == "completed":
                    status = "completed"
                elif state == "failed":
                    status = "failed"
                ev2: Dict[str, Json] = {
                    "type": "tool",
                    "role": "tool",
                    "tool": tool,
                    "callID": call_id,
                    "timestamp": ts,
                    "startedAt": ts,
                    "finishedAt": None,
                    "durationMs": dur,
                    "status": status,
                    "exitCode": exit_code,
                    "input": it.get("input") if isinstance(it.get("input"), dict) else {},
                    "output": it.get("output") if isinstance(it.get("output"), (dict, list, str)) else None,
                }
                if isinstance(dur, int) and ts_ms is not None:
                    ev2["finishedAt"] = _ms_to_iso(int(ts_ms + dur))
                out.append(ev2)

    for i in range(len(out) - 1, -1, -1):
        ev = out[i]
        if ev.get("type") == "text" and ev.get("role") == "assistant" and isinstance(ev.get("llm"), dict):
            ev["llm"]["usage"] = usage_total
            break

    return out


def run(
    *,
    prompt: str,
    work_dir: str,
    sandbox_dir: str,
    timeout_s: float,
    api_provider: Dict[str, Json],
    agent_id: Optional[str] = None,
) -> Dict[str, Json]:
    started_at = time.time()
    _ensure_dir(sandbox_dir)
    raw_dir = os.path.join(sandbox_dir, "raw")
    _ensure_dir(raw_dir)

    if not os.path.exists(CLAUDECODE_JS):
        return {"status": "error", "paths": [], "errorMessage": f"Missing ClaudeCode.js: {CLAUDECODE_JS}"}

    provider_type = api_provider.get("provider_type") if isinstance(api_provider, dict) else None
    base_url = api_provider.get("baseUrl") if isinstance(api_provider, dict) else None
    model = api_provider.get("model") if isinstance(api_provider, dict) else None
    api_key = api_provider.get("apiKey") if isinstance(api_provider, dict) else None
    model_name = api_provider.get("model_name") if isinstance(api_provider, dict) else None

    task_id = os.path.basename(os.path.abspath(sandbox_dir))
    cfg_path = os.path.join(raw_dir, "claudecode_config.json")
    report_path = os.path.join(raw_dir, "claudecode_report.json")

    cfg = {
        "description": f"claudecode run: {task_id}",
        "tasks": [
            {
                "id": task_id,
                "name": task_id,
                "prompt": str(prompt or ""),
                "cwd": os.path.abspath(work_dir),
                "timeout": int(timeout_s) if isinstance(timeout_s, (int, float)) else 300,
                "provider": str(provider_type) if isinstance(provider_type, str) else None,
                "model": str(model_name) if isinstance(model_name, str) else (str(model) if isinstance(model, str) else None),
                "customProvider": {
                    "baseUrl": str(base_url) if isinstance(base_url, str) else None,
                    "apiKey": str(api_key) if isinstance(api_key, str) else None,
                    "modelName": str(model) if isinstance(model, str) else (str(model_name) if isinstance(model_name, str) else None),
                },
            }
        ],
    }
    _write_json(cfg_path, cfg)

    env = os.environ.copy()
    if isinstance(provider_type, str) and provider_type.strip().lower() == "anthropic":
        if isinstance(api_key, str) and api_key.strip():
            env["ANTHROPIC_AUTH_TOKEN"] = api_key.strip()
            env["ANTHROPIC_API_KEY"] = api_key.strip()
        if isinstance(base_url, str) and base_url.strip():
            env["ANTHROPIC_BASE_URL"] = base_url.strip()
        if isinstance(model, str) and model.strip():
            env["ANTHROPIC_MODEL"] = model.strip()

    cmd = ["node", CLAUDECODE_JS, cfg_path, "-o", report_path]
    used_timeout = timeout_s if isinstance(timeout_s, (int, float)) and timeout_s > 0 else None

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=os.path.abspath(os.path.join(BASELINES_DIR, "..")),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout_text, stderr_text = proc.communicate(timeout=used_timeout)
            exit_code = int(proc.returncode or 0)
        except subprocess.TimeoutExpired:
            exit_code = 124
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                stdout_text, stderr_text = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
                stdout_text, stderr_text = proc.communicate()
    except Exception as e:
        exit_code = 1
        stdout_text = ""
        stderr_text = str(e)

    if isinstance(stdout_text, (bytes, bytearray)):
        try:
            stdout_text = stdout_text.decode("utf-8", errors="ignore")
        except Exception:
            stdout_text = ""
    if isinstance(stderr_text, (bytes, bytearray)):
        try:
            stderr_text = stderr_text.decode("utf-8", errors="ignore")
        except Exception:
            stderr_text = ""

    with open(os.path.join(raw_dir, "runner_stdout.txt"), "w", encoding="utf-8") as f:
        f.write(stdout_text)
    with open(os.path.join(raw_dir, "runner_stderr.txt"), "w", encoding="utf-8") as f:
        f.write(stderr_text)
    with open(os.path.join(raw_dir, "stdout.txt"), "w", encoding="utf-8") as f:
        f.write(stdout_text)
    with open(os.path.join(raw_dir, "stderr.txt"), "w", encoding="utf-8") as f:
        f.write(stderr_text)

    if not os.path.exists(report_path) or not os.path.isfile(report_path):
        status = "timeout" if exit_code == 124 else "error"
        return {
            "status": status,
            "paths": [],
            "errorMessage": (f"Timeout after {timeout_s}s" if status == "timeout" else (stderr_text[:2000] if isinstance(stderr_text, str) else "claudecode runner failed")),
            "trace": {"runner": "claudecode", "rawDir": raw_dir, "lastText": ""},
            "metrics": {"turns": None, "promptTokens": None, "completionTokens": None, "totalTokens": None},
            "durationMs": int((time.time() - started_at) * 1000),
        }

    report = _read_json(report_path)
    tasks = report.get("tasks") if isinstance(report, dict) else None
    tr = tasks[0] if isinstance(tasks, list) and tasks and isinstance(tasks[0], dict) else {}

    st = str(tr.get("status") or "").strip().lower()
    status = "ok"
    if st == "timeout":
        status = "timeout"
    elif st != "passed":
        status = "error"

    usage_total, provider2, model2 = _parse_usage_from_stdout(tr.get("stdout") if isinstance(tr.get("stdout"), str) else "")
    llm_provider = provider2 or (str(provider_type) if isinstance(provider_type, str) else None)
    llm_model = model2 or (str(model) if isinstance(model, str) else None)

    started_ms = int(started_at * 1000)
    execution_trace = _build_execution_trace(
        prompt=str(prompt or ""),
        started_ms=started_ms,
        task_result=tr if isinstance(tr, dict) else {},
        usage_total=usage_total,
        llm_base_url=str(base_url) if isinstance(base_url, str) else None,
        llm_model=llm_model,
        llm_provider=llm_provider,
    )

    last_text = ""
    tos = tr.get("textOutputs") if isinstance(tr, dict) else None
    if isinstance(tos, list) and tos and isinstance(tos[-1], str):
        last_text = str(tos[-1])

    metrics = {
        "turns": sum(1 for x in execution_trace if isinstance(x, dict) and x.get("type") == "text" and x.get("role") == "assistant"),
        "promptTokens": usage_total.get("prompt_tokens"),
        "completionTokens": usage_total.get("completion_tokens"),
        "totalTokens": usage_total.get("total_tokens"),
    }

    err_msg = None
    if status == "timeout":
        err_msg = f"Timeout after {timeout_s}s"
    elif status == "error":
        em = tr.get("errorMessage") if isinstance(tr, dict) else None
        err_msg = str(em) if isinstance(em, str) and em else (stderr_text[:2000] if isinstance(stderr_text, str) else "claudecode error")

    return {
        "status": status,
        "paths": [],
        "errorMessage": err_msg,
        "trace": {
            "runner": "claudecode",
            "agentId": agent_id,
            "rawDir": raw_dir,
            "lastText": last_text,
            "executionTrace": execution_trace,
            "llm": {"provider": llm_provider, "baseUrl": str(base_url) if isinstance(base_url, str) else None, "model": llm_model},
            "usageTotal": usage_total,
        },
        "metrics": metrics,
        "durationMs": int((time.time() - started_at) * 1000),
    }
