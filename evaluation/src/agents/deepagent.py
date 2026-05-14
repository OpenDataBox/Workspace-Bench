import ast
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

Json = Any

def _default_rip_bench_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


DEEPAGENTS_ROOT = os.path.abspath(
    os.environ.get("DEEPAGENTS_ROOT")
    or os.path.join(os.environ.get("WORKSPACE_BENCH_ROOT") or os.environ.get("RIP_BENCH_ROOT") or _default_rip_bench_root(), "deepagents")
)
DEEPAGENTS_LIB_PROJECT = os.path.join(DEEPAGENTS_ROOT, "libs", "deepagents")


def _ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)


def _write_json(path: str, obj: Json) -> None:
    _ensure_dir(os.path.dirname(os.path.abspath(path)))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _read_json(path: str) -> Json:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _iso_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _normalize_base_url(base_url: Optional[str]) -> Optional[str]:
    if not isinstance(base_url, str) or not base_url.strip():
        return None
    s = base_url.strip().rstrip("/")
    if "/api/compatible" in s and "/api/v3" not in s:
        s = s.replace("/api/compatible", "/api/v3")
    return s


def _extract_returned_paths(text: str) -> List[str]:
    s = str(text or "").strip()
    if not s:
        return []
    # Prefer the last bracketed Python list in the text (models sometimes include
    # extra prose or reasoning before/after the list).
    candidates = re.findall(r"\[[\s\S]*?\]", s)
    for cand in reversed(candidates or [s]):
        try:
            obj = ast.literal_eval(cand)
        except Exception:
            continue
        if isinstance(obj, list):
            out = [str(x) for x in obj if isinstance(x, str) and str(x).strip()]
            if out:
                return out
    return []


def _resolve_under(root: str, p: str) -> Optional[str]:
    try:
        rel = str(p or "").strip().replace("\\", "/")
        while rel.startswith("/"):
            rel = rel[1:]
        abs_p = os.path.abspath(os.path.join(root, rel))
        root_abs = os.path.abspath(root)
        if abs_p != root_abs and not abs_p.startswith(root_abs + os.sep):
            return None
        return abs_p
    except Exception:
        return None


def _usage_from_msg(msg: Dict[str, Json]) -> Dict[str, int]:
    usage = msg.get("usage_metadata") if isinstance(msg.get("usage_metadata"), dict) else {}
    pt = usage.get("input_tokens") if isinstance(usage.get("input_tokens"), int) else 0
    ct = usage.get("output_tokens") if isinstance(usage.get("output_tokens"), int) else 0
    return {
        "prompt_tokens": int(pt),
        "completion_tokens": int(ct),
        "total_tokens": int(pt) + int(ct),
        "cache_read": int(usage.get("input_token_details", {}).get("cache_read", 0)) if isinstance(usage.get("input_token_details"), dict) else 0,
        "cache_write": 0,
    }


def _msg_text_content(content: Json) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(str(item.get("text")))
        return "\n".join([x for x in parts if x])
    return ""


def _tool_output_payload(msg: Dict[str, Json]) -> Dict[str, Json]:
    out: Dict[str, Json] = {
        "content": msg.get("content"),
        "artifact": msg.get("artifact"),
    }
    if isinstance(msg.get("name"), str):
        out["name"] = str(msg.get("name"))
    return out


def _stop_reason_from_msg(msg: Dict[str, Json]) -> Optional[str]:
    meta = msg.get("response_metadata") if isinstance(msg.get("response_metadata"), dict) else {}
    for key in ("stop_reason", "finish_reason", "stopReason"):
        val = meta.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def _build_execution_trace(
    *,
    prompt: str,
    started_at: float,
    messages: List[Dict[str, Json]],
    base_url: Optional[str],
    model: Optional[str],
    provider: str,
) -> Dict[str, Json]:
    execution_trace: List[Dict[str, Json]] = [
        {
            "type": "text",
            "role": "user",
            "content": str(prompt or ""),
            "timestamp": _iso_from_ts(started_at),
        }
    ]
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cache_read": 0, "cache_write": 0}
    tool_idx: Dict[str, Dict[str, Json]] = {}
    turn = 0
    now_ts = started_at
    last_text = ""

    for msg in messages[1:]:
        if not isinstance(msg, dict):
            continue
        msg_type = str(msg.get("type") or "")
        now_ts += 0.001
        ts = _iso_from_ts(now_ts)

        if msg_type == "ai":
            usage = _usage_from_msg(msg)
            usage_total["prompt_tokens"] += usage["prompt_tokens"]
            usage_total["completion_tokens"] += usage["completion_tokens"]
            usage_total["total_tokens"] += usage["total_tokens"]
            usage_total["cache_read"] += usage["cache_read"]

            content = _msg_text_content(msg.get("content"))
            tool_calls = msg.get("tool_calls") if isinstance(msg.get("tool_calls"), list) else []
            # Mirror ClaudeCode more closely: keep every AI turn, even when it only
            # contains tool calls and no visible text.
            if content or tool_calls:
                turn += 1
                if content:
                    last_text = content
                execution_trace.append(
                    {
                        "type": "text",
                        "role": "assistant",
                        "content": content,
                        "timestamp": ts,
                        "turn": turn,
                        "llm": {
                            "provider": provider,
                            "baseUrl": base_url,
                            "model": model,
                            "usage": usage,
                            "stopReason": _stop_reason_from_msg(msg),
                            "errorMessage": None,
                        },
                    }
                )

            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                call_id = str(tc.get("id") or f"deepagents_tool_{len(tool_idx)+1}")
                ev = {
                    "type": "tool",
                    "role": "tool",
                    "tool": tc.get("name"),
                    "callID": call_id,
                    "timestamp": ts,
                    "startedAt": ts,
                    "finishedAt": None,
                    "durationMs": None,
                    "status": None,
                    "exitCode": None,
                    "input": tc.get("args") if isinstance(tc.get("args"), dict) else {},
                    "output": None,
                    "turn": turn,
                }
                execution_trace.append(ev)
                tool_idx[call_id] = ev

        elif msg_type == "tool":
            call_id = msg.get("tool_call_id") if isinstance(msg.get("tool_call_id"), str) else None
            ev = tool_idx.get(call_id) if call_id else None
            if ev is None:
                synthetic_id = f"deepagents_orphan_{len(tool_idx)+1}"
                ev = {
                    "type": "tool",
                    "role": "tool",
                    "tool": msg.get("name"),
                    "callID": synthetic_id,
                    "timestamp": ts,
                    "startedAt": ts,
                    "finishedAt": ts,
                    "durationMs": 0,
                    "status": "completed",
                    "exitCode": None,
                    "input": {},
                    "output": _tool_output_payload(msg),
                }
                execution_trace.append(ev)
                tool_idx[synthetic_id] = ev
                continue
            ev["finishedAt"] = ts
            ev["durationMs"] = 0
            ev["status"] = "completed"
            ev["output"] = _tool_output_payload(msg)

    # Attach aggregated usage to the last assistant text if present.
    for ev in range(len(execution_trace) - 1, -1, -1):
        item = execution_trace[ev]
        if item.get("type") == "text" and item.get("role") == "assistant" and isinstance(item.get("llm"), dict):
            item["llm"]["usage"] = dict(usage_total)
            break

    return {
        "executionTrace": execution_trace,
        "lastText": last_text,
        "usageTotal": usage_total,
    }


def _runner_script() -> str:
    return """import json
import os
import sys
import time

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage

from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend


def _is_retryable_error(err: Exception) -> bool:
    s = str(err)
    # Common overload / rate-limit signals from OpenAI-compatible endpoints.
    if "overloaded_error" in s:
        return True
    if "Error code: 529" in s:
        return True
    # Avoid embedding quoted JSON snippets in this script (can break generation).
    if ("http_code" in s or "httpCode" in s) and "529" in s:
        return True
    if "Error code: 429" in s:
        return True
    if "rate limit" in s.lower():
        return True
    if "temporarily unavailable" in s.lower():
        return True
    return False


def safe_json(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): safe_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [safe_json(v) for v in value]
    if hasattr(value, "model_dump"):
        try:
            return safe_json(value.model_dump())
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        try:
            return safe_json(vars(value))
        except Exception:
            pass
    return repr(value)


def serialize_msg(msg):
    return {
        "class": type(msg).__name__,
        "type": getattr(msg, "type", None),
        "id": getattr(msg, "id", None),
        "name": getattr(msg, "name", None),
        "content": safe_json(getattr(msg, "content", None)),
        "tool_calls": safe_json(getattr(msg, "tool_calls", None)),
        "tool_call_id": getattr(msg, "tool_call_id", None),
        "artifact": safe_json(getattr(msg, "artifact", None)),
        "usage_metadata": safe_json(getattr(msg, "usage_metadata", None)),
        "response_metadata": safe_json(getattr(msg, "response_metadata", None)),
    }


def main():
    cfg_path = sys.argv[1]
    out_path = sys.argv[2]
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    model = init_chat_model(
        f"openai:{cfg['model']}",
        base_url=cfg.get("base_url"),
        api_key=cfg.get("api_key"),
        use_responses_api=False,
    )
    backend = LocalShellBackend(
        root_dir=cfg["work_dir"],
        virtual_mode=True,
        inherit_env=True,
        timeout=int(cfg.get("timeout_s") or 120),
    )
    agent = create_deep_agent(model=model, backend=backend)
    result = None
    err = None
    retries = int(cfg.get("max_retries") or 3)
    base_sleep = float(cfg.get("retry_sleep_s") or 2.0)
    for attempt in range(retries + 1):
        try:
            result = agent.invoke({"messages": [HumanMessage(content=cfg["prompt"])]})
            err = None
            break
        except Exception as e:
            err = e
            if attempt >= retries or not _is_retryable_error(e):
                break
            # Exponential backoff (capped) to smooth out overload bursts.
            sleep_s = min(30.0, base_sleep * (2**attempt))
            time.sleep(sleep_s)

    messages = result.get("messages") if isinstance(result, dict) else None
    if not isinstance(messages, list):
        messages = []

    final_text = ""
    for m in reversed(messages):
        if getattr(m, "type", None) != "ai":
            continue
        content = getattr(m, "content", None)
        if isinstance(content, str) and content.strip():
            final_text = content.strip()
            break

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "messages": [serialize_msg(m) for m in messages],
                "final_text": final_text,
                "error": repr(err) if err is not None else None,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )


if __name__ == "__main__":
    main()
"""


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

    if not os.path.isdir(DEEPAGENTS_LIB_PROJECT):
        return {
            "status": "error",
            "paths": [],
            "errorMessage": f"Missing deepagents project: {DEEPAGENTS_LIB_PROJECT}",
        }

    base_url = _normalize_base_url(api_provider.get("baseUrl") if isinstance(api_provider, dict) else None)
    model = api_provider.get("model") if isinstance(api_provider, dict) else None
    api_key = api_provider.get("apiKey") if isinstance(api_provider, dict) else None
    if not isinstance(api_key, str) or not api_key.strip():
        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("ARK_API_KEY")
    if not isinstance(model, str) or not model.strip():
        return {"status": "error", "paths": [], "errorMessage": "Missing model in api_provider"}

    script_path = os.path.join(raw_dir, "run_deepagent.py")
    input_path = os.path.join(raw_dir, "input.json")
    result_path = os.path.join(raw_dir, "result.json")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(_runner_script())
    _write_json(
        input_path,
        {
            "prompt": str(prompt or ""),
            "work_dir": os.path.abspath(work_dir),
            "timeout_s": int(timeout_s) if isinstance(timeout_s, (int, float)) else 120,
            "base_url": base_url,
            "model": str(model),
            "api_key": str(api_key or ""),
            # OpenAI-compatible gateways may return transient 429/529. Default to a few
            # retries so eval runs are less flaky.
            "max_retries": 8,
            "retry_sleep_s": 2.0,
        },
    )

    cmd = [
        "uv",
        "run",
        "--project",
        DEEPAGENTS_LIB_PROJECT,
        "--with",
        "langchain-openai>=1.1.12,<2.0.0",
        "python",
        script_path,
        input_path,
        result_path,
    ]
    _write_json(
        os.path.join(raw_dir, "deepagent_invocation.json"),
        {
            "cmd": cmd,
            "cwd": os.path.abspath(work_dir),
            "baseUrl": base_url,
            "model": str(model),
        },
    )

    env = os.environ.copy()
    if isinstance(api_key, str):
        env["OPENAI_API_KEY"] = api_key
    if isinstance(base_url, str) and base_url:
        env["OPENAI_BASE_URL"] = base_url

    used_timeout = timeout_s if isinstance(timeout_s, (int, float)) and timeout_s > 0 else None
    stdout_text = ""
    stderr_text = ""
    exit_code = 1
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=os.path.abspath(work_dir),
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
        stderr_text = str(e)

    with open(os.path.join(raw_dir, "runner_stdout.txt"), "w", encoding="utf-8") as f:
        f.write(stdout_text or "")
    with open(os.path.join(raw_dir, "runner_stderr.txt"), "w", encoding="utf-8") as f:
        f.write(stderr_text or "")
    with open(os.path.join(raw_dir, "stdout.txt"), "w", encoding="utf-8") as f:
        f.write(stdout_text or "")
    with open(os.path.join(raw_dir, "stderr.txt"), "w", encoding="utf-8") as f:
        f.write(stderr_text or "")

    result_obj: Dict[str, Json] = {}
    if os.path.exists(result_path):
        try:
            result_obj = _read_json(result_path)
        except Exception:
            result_obj = {}

    script_error = result_obj.get("error") if isinstance(result_obj.get("error"), str) else None
    messages = result_obj.get("messages") if isinstance(result_obj.get("messages"), list) else []
    final_text = result_obj.get("final_text") if isinstance(result_obj.get("final_text"), str) else ""

    returned_paths_abs: List[str] = []
    for rel in _extract_returned_paths(final_text):
        ap = _resolve_under(os.path.abspath(work_dir), rel)
        if ap and os.path.exists(ap):
            returned_paths_abs.append(ap)

    status = "ok"
    err_msg = None
    if exit_code == 124:
        status = "timeout"
        err_msg = f"Timeout after {timeout_s}s"
    elif script_error:
        status = "error"
        err_msg = str(script_error)[:4000]
    elif exit_code != 0:
        status = "error"
        err_msg = (str(stderr_text or "").strip() or f"deepagents exit code {exit_code}")[:4000]

    trace_info = _build_execution_trace(
        prompt=str(prompt or ""),
        started_at=started_at,
        messages=[x for x in messages if isinstance(x, dict)],
        base_url=base_url,
        model=str(model),
        provider="openai",
    )
    usage_total = trace_info.get("usageTotal") if isinstance(trace_info.get("usageTotal"), dict) else {}
    execution_trace = trace_info.get("executionTrace") if isinstance(trace_info.get("executionTrace"), list) else []

    return {
        "status": status,
        "paths": sorted(set(returned_paths_abs)),
        "errorMessage": err_msg,
        "trace": {
            "runner": "deepagents",
            "agentId": agent_id,
            "rawDir": raw_dir,
            "lastText": trace_info.get("lastText") if isinstance(trace_info.get("lastText"), str) else "",
            "executionTrace": execution_trace,
            "llm": {
                "provider": "openai",
                "baseUrl": base_url,
                "model": str(model),
            },
            "usageTotal": usage_total,
        },
        "metrics": {
            "turns": sum(1 for x in execution_trace if isinstance(x, dict) and x.get("type") == "text" and x.get("role") == "assistant"),
            "promptTokens": usage_total.get("prompt_tokens") if isinstance(usage_total, dict) else None,
            "completionTokens": usage_total.get("completion_tokens") if isinstance(usage_total, dict) else None,
            "totalTokens": usage_total.get("total_tokens") if isinstance(usage_total, dict) else None,
        },
        "durationMs": int((time.time() - started_at) * 1000),
    }
