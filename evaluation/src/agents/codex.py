import json
import http.server
import os
import re
import shutil
import socketserver
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

Json = Any


def _ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)


def _write_json(path: str, obj: Json) -> None:
    _ensure_dir(os.path.dirname(os.path.abspath(path)))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


def _iso_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _jsonl_events(text: str) -> List[Dict[str, Json]]:
    out: List[Dict[str, Json]] = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _expand_provider_value(value: Json) -> Optional[str]:
    if not isinstance(value, str):
        return None
    s = value.strip()
    fallback_re = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)[:-]-(\$\{[A-Za-z_][A-Za-z0-9_]*\}|[^}]*)\}")
    while True:
        m = fallback_re.search(s)
        if not m:
            break
        primary = os.environ.get(m.group(1), "")
        fallback = m.group(2)
        repl = primary if primary else os.path.expandvars(fallback)
        s = s[: m.start()] + repl + s[m.end() :]
    s = os.path.expandvars(s).strip()
    # Treat unresolved placeholders from YAML templates as missing.
    if not s or re.search(r"\$\{[^}]+\}", s):
        return None
    return s


def _first_config_value(*values: Json) -> Optional[str]:
    for value in values:
        expanded = _expand_provider_value(value)
        if expanded:
            return expanded
    return None


def _load_dotenv() -> None:
    eval_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    for path in (os.path.join(eval_root, ".env"), os.path.join(os.getcwd(), ".env")):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            continue
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _codex_sandbox_mode() -> str:
    mode = str(os.environ.get("CODEX_SANDBOX_MODE") or "workspace-write").strip()
    if mode in {"read-only", "workspace-write", "danger-full-access"}:
        return mode
    return "workspace-write"


def _normalize_base_url(base_url: Optional[str]) -> str:
    if isinstance(base_url, str) and base_url.strip():
        return base_url.strip().rstrip("/")
    return "https://api.openai.com/v1"


def _normalize_usage(obj: Json) -> Dict[str, int]:
    if not isinstance(obj, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cache_read": 0, "cache_write": 0}

    usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else obj
    prompt_tokens = usage.get("input_tokens")
    if not isinstance(prompt_tokens, int):
        prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("output_tokens")
    if not isinstance(completion_tokens, int):
        completion_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens")

    prompt_tokens = int(prompt_tokens or 0)
    completion_tokens = int(completion_tokens or 0)
    total_tokens = int(total_tokens or (prompt_tokens + completion_tokens))

    input_details = usage.get("input_token_details") if isinstance(usage.get("input_token_details"), dict) else {}
    prompt_details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    completion_details = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cache_read": int(usage.get("cached_input_tokens") or input_details.get("cache_read") or prompt_details.get("cached_tokens") or 0),
        "cache_write": int(usage.get("reasoning_output_tokens") or completion_details.get("reasoning_tokens") or 0),
    }


def _add_usage(dst: Dict[str, int], usage: Dict[str, int]) -> None:
    for k in ("prompt_tokens", "completion_tokens", "total_tokens", "cache_read", "cache_write"):
        dst[k] = int(dst.get(k) or 0) + int(usage.get(k) or 0)


def _text_from_content(content: Json) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                txt = item.get("text") or item.get("content")
                if isinstance(txt, str):
                    parts.append(txt)
        return "\n".join([x for x in parts if x])
    if isinstance(content, dict):
        txt = content.get("text") or content.get("content")
        if isinstance(txt, str):
            return txt
    return ""


def _extract_tool_payload(evt: Dict[str, Json]) -> Optional[Dict[str, Json]]:
    for key in ("tool_call", "toolCall", "call", "item", "data", "payload"):
        val = evt.get(key)
        if isinstance(val, dict):
            name = val.get("tool") or val.get("tool_name") or val.get("name")
            args = val.get("arguments") or val.get("args") or val.get("input")
            if name or args:
                return {
                    "tool": name if isinstance(name, str) else None,
                    "callID": val.get("call_id") or val.get("callId") or val.get("id"),
                    "input": args if isinstance(args, dict) else {},
                    "output": val.get("output") if "output" in val else val.get("result"),
                    "exitCode": val.get("exit_code") or val.get("exitCode"),
                }
    return None


def parse_codex_jsonl(stdout_text: str, *, prompt: str, started_at: float, base_url: Optional[str], model: Optional[str]) -> Dict[str, Json]:
    events = _jsonl_events(stdout_text)
    execution_trace: List[Dict[str, Json]] = [
        {"type": "text", "role": "user", "content": str(prompt or ""), "timestamp": _iso_from_ts(started_at)}
    ]
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cache_read": 0, "cache_write": 0}
    last_text = ""
    turns = 0

    for idx, evt in enumerate(events):
        typ = str(evt.get("type") or evt.get("event") or "")
        item = evt.get("item") if isinstance(evt.get("item"), dict) else {}
        item_type = str(item.get("type") or "")
        ts = _iso_from_ts(started_at + ((idx + 1) / 1000.0))

        usage = None
        if isinstance(evt.get("usage"), dict):
            usage = _normalize_usage(evt.get("usage"))
        elif isinstance(evt.get("usageTotal"), dict):
            usage = _normalize_usage(evt.get("usageTotal"))
        if usage:
            _add_usage(usage_total, usage)

        text = ""
        if typ in {"item.completed", "item.started"} and item_type == "agent_message":
            text = _text_from_content(item.get("text") or item.get("message") or item.get("content"))
        elif typ in {"agent_message", "agent_reasoning", "message", "turn_complete", "task_complete"}:
            text = _text_from_content(evt.get("message") or evt.get("content") or evt.get("text") or evt.get("last_agent_message"))
        elif typ in {"response.output_text.delta", "output_text.delta", "text"}:
            text = _text_from_content(evt.get("delta") or evt.get("text") or evt.get("content"))
        elif typ in {"result", "response.completed"}:
            text = _text_from_content(evt.get("result") or evt.get("output") or evt.get("message"))

        if text.strip():
            turns += 1
            last_text = text.strip()
            execution_trace.append(
                {
                    "type": "text",
                    "role": "assistant",
                    "content": text,
                    "timestamp": ts,
                    "turn": turns,
                    "llm": {
                        "provider": "codex",
                        "baseUrl": base_url,
                        "model": model,
                        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cache_read": 0, "cache_write": 0},
                        "stopReason": evt.get("stop_reason") or evt.get("finish_reason"),
                        "errorMessage": evt.get("error") if isinstance(evt.get("error"), str) else None,
                    },
                }
            )

        tool_payload = _extract_tool_payload(evt)
        if typ in {"item.completed", "item.started"} and item_type in {"command_execution", "mcp_tool_call"}:
            tool_payload = {
                "tool": item_type,
                "callID": item.get("id"),
                "input": {"command": item.get("command")} if isinstance(item.get("command"), str) else {},
                "output": item.get("aggregated_output") if "aggregated_output" in item else item.get("output"),
                "exitCode": item.get("exit_code"),
            }
        if tool_payload and (
            "tool" in typ
            or typ in {"item.completed", "item.started", "exec_command_begin", "exec_command_end", "mcp_tool_call_begin", "mcp_tool_call_end"}
        ):
            is_done = typ in {"item.completed", "exec_command_end", "mcp_tool_call_end"} or typ.endswith("_end") or typ.endswith(".end")
            execution_trace.append(
                {
                    "type": "tool",
                    "role": "tool",
                    "tool": tool_payload.get("tool"),
                    "callID": tool_payload.get("callID"),
                    "timestamp": ts,
                    "startedAt": ts,
                    "finishedAt": ts if is_done else None,
                    "durationMs": None,
                    "status": "completed" if is_done else "in_progress",
                    "exitCode": tool_payload.get("exitCode"),
                    "input": tool_payload.get("input") if isinstance(tool_payload.get("input"), dict) else {},
                    "output": tool_payload.get("output"),
                    "turn": turns or None,
                }
            )

    return {
        "executionTrace": execution_trace,
        "lastText": last_text,
        "usageTotal": usage_total,
        "turns": turns,
        "events": len(events),
    }


def _toml_str(value: str) -> str:
    return json.dumps(str(value))


def _provider_config_arg(*, base_url: str) -> str:
    return "{name=\"ripbench\", base_url=" + _toml_str(base_url) + ", env_key=\"CODEX_API_KEY\", wire_api=\"responses\"}"


def _should_use_chat_adapter(model: str) -> bool:
    mode = (os.environ.get("CODEX_CHAT_ADAPTER") or "auto").strip().lower()
    if mode in {"0", "false", "no", "never", "off"}:
        return False
    if mode in {"1", "true", "yes", "always", "on"}:
        return True
    return not str(model or "").lower().startswith("gpt-")


def _responses_text(content: Json) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                txt = item.get("text")
                if isinstance(txt, str):
                    parts.append(txt)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join([x for x in parts if x])
    return ""


def _responses_input_to_chat(input_items: Json) -> List[Dict[str, Json]]:
    messages: List[Dict[str, Json]] = []
    pending_tool_calls: List[Dict[str, Json]] = []
    if not isinstance(input_items, list):
        return messages
    for item in input_items:
        if not isinstance(item, dict):
            continue
        typ = item.get("type")
        if typ == "message":
            role = str(item.get("role") or "user")
            if role == "developer":
                role = "system"
            if role not in {"system", "user", "assistant", "tool"}:
                role = "user"
            messages.append({"role": role, "content": _responses_text(item.get("content"))})
        elif typ == "function_call":
            args = item.get("arguments")
            if not isinstance(args, str):
                args = json.dumps(args or {}, ensure_ascii=False)
            pending_tool_calls.append(
                {
                    "id": str(item.get("call_id") or item.get("id") or f"call_{len(pending_tool_calls)}"),
                    "type": "function",
                    "function": {"name": str(item.get("name") or "unknown"), "arguments": args},
                }
            )
        elif typ == "function_call_output":
            if pending_tool_calls:
                messages.append({"role": "assistant", "content": "", "tool_calls": pending_tool_calls})
                pending_tool_calls = []
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(item.get("call_id") or ""),
                    "content": _responses_text(item.get("output")) or str(item.get("output") or ""),
                }
            )
    if pending_tool_calls:
        messages.append({"role": "assistant", "content": "", "tool_calls": pending_tool_calls})
    return messages


def _responses_tools_to_chat(tools: Json) -> List[Dict[str, Json]]:
    out: List[Dict[str, Json]] = []
    if not isinstance(tools, list):
        return out
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        out.append(
            {
                "type": "function",
                "function": {
                    "name": str(tool.get("name") or ""),
                    "description": str(tool.get("description") or ""),
                    "parameters": tool.get("parameters") if isinstance(tool.get("parameters"), dict) else {"type": "object", "properties": {}},
                },
            }
        )
    return [x for x in out if x["function"]["name"]]


def _chat_usage_to_responses(usage: Json) -> Dict[str, int]:
    if not isinstance(usage, dict):
        usage = {}
    return {
        "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }


def _adapter_max_tokens(req_obj: Dict[str, Json]) -> int:
    raw = req_obj.get("max_output_tokens") or os.environ.get("CODEX_CHAT_ADAPTER_MAX_TOKENS") or 16384
    try:
        value = int(raw)
    except Exception:
        value = 16384
    return max(1024, min(value, 65536))


def _extract_text_tool_calls(text: str, valid_tools: List[str]) -> List[Dict[str, Json]]:
    if not isinstance(text, str) or "<invoke" not in text:
        return []
    valid = {name for name in valid_tools if name}
    calls: List[Dict[str, Json]] = []
    for idx, match in enumerate(re.finditer(r"<invoke\s+name=[\"']([^\"']+)[\"']\s*>(.*?)</invoke>", text, flags=re.DOTALL)):
        name = match.group(1).strip()
        if valid and name not in valid:
            continue
        body = match.group(2)
        params: Dict[str, str] = {}
        for pm in re.finditer(r"<parameter\s+name=[\"']([^\"']+)[\"']\s*>(.*?)</parameter>", body, flags=re.DOTALL):
            params[pm.group(1).strip()] = pm.group(2)
        calls.append(
            {
                "id": f"text_call_{idx}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(params, ensure_ascii=False)},
            }
        )
    return calls


def _sse_event(event: Dict[str, Json]) -> bytes:
    return ("data: " + json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode("utf-8")


def _start_chat_adapter(*, target_base_url: str, api_key: str, model: str, raw_dir: str) -> Tuple[socketserver.BaseServer, str]:
    target = target_base_url.rstrip("/")
    log_path = os.path.join(raw_dir, "chat_adapter_log.jsonl")

    class _ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True
        daemon_threads = True

    class _Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            return

        def _write_log(self, obj: Dict[str, Json]) -> None:
            _ensure_dir(os.path.dirname(log_path))
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")

        def do_POST(self) -> None:
            started = time.time()
            try:
                length = int(self.headers.get("Content-Length") or 0)
                req_obj = json.loads(self.rfile.read(length).decode("utf-8", errors="ignore") or "{}")
                messages = _responses_input_to_chat(req_obj.get("input"))
                if not messages:
                    messages = [{"role": "user", "content": ""}]
                chat_body: Dict[str, Json] = {
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "max_tokens": _adapter_max_tokens(req_obj),
                }
                tools = _responses_tools_to_chat(req_obj.get("tools"))
                tool_names = [str(t.get("function", {}).get("name") or "") for t in tools if isinstance(t.get("function"), dict)]
                if tools:
                    chat_body["tools"] = tools
                    chat_body["tool_choice"] = "auto"

                upstream_req = urllib.request.Request(
                    target + "/chat/completions",
                    data=json.dumps(chat_body, ensure_ascii=False).encode("utf-8"),
                    method="POST",
                    headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(upstream_req, timeout=300) as resp:
                    upstream_obj = json.loads(resp.read().decode("utf-8", errors="ignore") or "{}")
                choice = (upstream_obj.get("choices") if isinstance(upstream_obj.get("choices"), list) else [{}])[0]
                msg = choice.get("message") if isinstance(choice.get("message"), dict) else {}
                usage = _chat_usage_to_responses(upstream_obj.get("usage"))
                rid = str(upstream_obj.get("id") or f"resp_{int(time.time() * 1000)}")

                output_items: List[Dict[str, Json]] = []
                events: List[Dict[str, Json]] = [{"type": "response.created", "response": {"id": rid, "status": "in_progress"}}]
                tool_calls = msg.get("tool_calls") if isinstance(msg.get("tool_calls"), list) else []
                text = msg.get("content")
                if not isinstance(text, str) or not text:
                    text = msg.get("reasoning_content") if isinstance(msg.get("reasoning_content"), str) else ""
                if not tool_calls:
                    tool_calls = _extract_text_tool_calls(text, tool_names)
                if tool_calls:
                    for i, tc in enumerate(tool_calls):
                        func = tc.get("function") if isinstance(tc, dict) and isinstance(tc.get("function"), dict) else {}
                        args = func.get("arguments") if isinstance(func.get("arguments"), str) else "{}"
                        item = {
                            "id": f"fc_{i}",
                            "type": "function_call",
                            "call_id": str(tc.get("id") or f"call_{i}"),
                            "name": str(func.get("name") or ""),
                            "arguments": args,
                        }
                        output_items.append(item)
                        events.append({"type": "response.output_item.added", "output_index": i, "item": {**item, "arguments": ""}})
                        if args:
                            events.append({"type": "response.function_call_arguments.delta", "item_id": item["id"], "output_index": i, "delta": args})
                        events.append({"type": "response.output_item.done", "output_index": i, "item": item})
                else:
                    item = {"id": "msg_0", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}
                    output_items.append(item)
                    events.extend(
                        [
                            {"type": "response.output_item.added", "output_index": 0, "item": {"id": "msg_0", "type": "message", "role": "assistant", "content": []}},
                            {"type": "response.content_part.added", "item_id": "msg_0", "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": ""}},
                            {"type": "response.output_text.delta", "item_id": "msg_0", "output_index": 0, "content_index": 0, "delta": text},
                            {"type": "response.output_text.done", "item_id": "msg_0", "output_index": 0, "content_index": 0, "text": text},
                            {"type": "response.content_part.done", "item_id": "msg_0", "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": text}},
                            {"type": "response.output_item.done", "output_index": 0, "item": item},
                        ]
                    )
                events.append({"type": "response.completed", "response": {"id": rid, "status": "completed", "output": output_items, "usage": usage}})

                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                for ev in events:
                    self.wfile.write(_sse_event(ev))
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self._write_log(
                    {
                        "status": 200,
                        "durationMs": int((time.time() - started) * 1000),
                        "model": model,
                        "messages": len(messages),
                        "tools": len(tools),
                        "toolNames": tool_names,
                        "finishReason": choice.get("finish_reason"),
                        "contentHead": text[:300] if isinstance(text, str) else "",
                        "outputItems": len(output_items),
                    }
                )
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="ignore")
                self._write_log({"status": int(e.code), "error": body[:2000], "durationMs": int((time.time() - started) * 1000)})
                self.send_response(int(e.code))
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            except Exception as e:
                self._write_log({"status": 500, "error": str(e), "durationMs": int((time.time() - started) * 1000)})
                body = json.dumps({"error": {"message": str(e)}}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

    server = _ThreadingServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


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
    _load_dotenv()
    _ensure_dir(sandbox_dir)
    raw_dir = os.path.join(sandbox_dir, "raw")
    _ensure_dir(raw_dir)

    codex_bin = shutil.which("codex")
    if not codex_bin:
        return {
            "status": "error",
            "paths": [],
            "errorMessage": "Missing codex CLI in PATH",
            "trace": {"runner": "codex", "rawDir": raw_dir, "lastText": ""},
            "metrics": {"turns": None, "promptTokens": None, "completionTokens": None, "totalTokens": None},
            "durationMs": int((time.time() - started_at) * 1000),
        }

    base_url = _normalize_base_url(
        _first_config_value(
            api_provider.get("baseUrl") if isinstance(api_provider, dict) else None,
            api_provider.get("base_url") if isinstance(api_provider, dict) else None,
            os.environ.get("CODEX_BASE_URL"),
        )
    )
    model = _first_config_value(api_provider.get("model") if isinstance(api_provider, dict) else None)
    api_key = _first_config_value(
        api_provider.get("apiKey") if isinstance(api_provider, dict) else None,
        api_provider.get("api_key") if isinstance(api_provider, dict) else None,
        os.environ.get("CODEX_API_KEY"),
        os.environ.get("OPENAI_API_KEY"),
    )
    if not api_key:
        api_key = None
    if not model:
        return {"status": "error", "paths": [], "errorMessage": "Missing model in api_provider"}
    if not api_key:
        return {"status": "error", "paths": [], "errorMessage": "Missing CODEX_API_KEY/apiProvider.apiKey for Codex harness"}

    adapter_server = None
    provider_base_url = base_url
    provider_api_key = api_key
    if _should_use_chat_adapter(model):
        adapter_server, provider_base_url = _start_chat_adapter(target_base_url=base_url, api_key=api_key, model=model, raw_dir=raw_dir)
        provider_api_key = "local-adapter"

    last_message_path = os.path.join(raw_dir, "last_message.txt")
    cmd = [
        codex_bin,
        "-a",
        "never",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--ephemeral",
        "--cd",
        os.path.abspath(work_dir),
        "--sandbox",
        _codex_sandbox_mode(),
        "--output-last-message",
        last_message_path,
        "--model",
        model,
        "-c",
        "model_provider=\"ripbench\"",
        "-c",
        f"model_providers.ripbench={_provider_config_arg(base_url=provider_base_url)}",
        "-",
    ]
    _write_json(
        os.path.join(raw_dir, "codex_invocation.json"),
        {
            "cmd": cmd,
            "cwd": os.path.abspath(work_dir),
            "baseUrl": base_url,
            "providerBaseUrl": provider_base_url,
            "model": model,
            "agentId": agent_id,
            "chatAdapter": bool(adapter_server),
        },
    )

    env = os.environ.copy()
    env["CODEX_API_KEY"] = provider_api_key
    used_timeout = timeout_s if isinstance(timeout_s, (int, float)) and timeout_s > 0 else None
    stdout_text = ""
    stderr_text = ""
    exit_code = 1
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=os.path.abspath(work_dir),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout_text, stderr_text = proc.communicate(input=str(prompt or ""), timeout=used_timeout)
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
        stderr_text = str(e)
        exit_code = 1
    finally:
        if adapter_server is not None:
            try:
                adapter_server.shutdown()
                adapter_server.server_close()
            except Exception:
                pass

    # A case directory can be moved or recreated while other runs are cleaning up.
    # Recreate raw_dir here so one missing path does not crash the whole runner.
    _ensure_dir(raw_dir)
    for name, text in {
        "codex_stdout.jsonl": stdout_text or "",
        "runner_stdout.txt": stdout_text or "",
        "stdout.txt": stdout_text or "",
        "runner_stderr.txt": stderr_text or "",
        "stderr.txt": stderr_text or "",
    }.items():
        with open(os.path.join(raw_dir, name), "w", encoding="utf-8") as f:
            f.write(text)

    last_text_file = _read_text(last_message_path).strip()
    trace_core = parse_codex_jsonl(stdout_text or "", prompt=str(prompt or ""), started_at=started_at, base_url=base_url, model=model)
    if last_text_file:
        trace_core["lastText"] = last_text_file

    status = "ok"
    error_message = None
    if exit_code == 124:
        status = "timeout"
        error_message = f"Timeout after {timeout_s}s"
    elif exit_code != 0:
        status = "error"
        error_message = (stderr_text or stdout_text or "codex runner failed")[:4000]

    usage_total = trace_core.get("usageTotal") if isinstance(trace_core.get("usageTotal"), dict) else {}
    metrics = {
        "turns": trace_core.get("turns") if isinstance(trace_core.get("turns"), int) else None,
        "promptTokens": int(usage_total.get("prompt_tokens") or 0),
        "completionTokens": int(usage_total.get("completion_tokens") or 0),
        "totalTokens": int(usage_total.get("total_tokens") or 0),
    }

    return {
        "status": status,
        "paths": [],
        "errorMessage": error_message,
        "trace": {
            "runner": "codex",
            "agentId": agent_id,
            "rawDir": raw_dir,
            "lastText": str(trace_core.get("lastText") or ""),
            "executionTrace": trace_core.get("executionTrace") if isinstance(trace_core.get("executionTrace"), list) else [],
            "llm": {"provider": "codex", "baseUrl": base_url, "model": model},
            "usageTotal": usage_total,
            "events": trace_core.get("events"),
        },
        "metrics": metrics,
        "durationMs": int((time.time() - started_at) * 1000),
    }
