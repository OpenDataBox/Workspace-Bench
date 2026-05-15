import json
import http.server
import os
import shutil
import socketserver
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

Json = Any


def _ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)


def _read_json(path: str) -> Json:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: str, obj: Json) -> None:
    _ensure_dir(os.path.dirname(os.path.abspath(path)))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _json_first_value(text: str) -> Optional[Json]:
    s = str(text or "").lstrip()
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


def _normalize_rel_path(p: str) -> str:
    s = str(p or "").strip().replace("\\", "/")
    while s.startswith("/"):
        s = s[1:]
    return s


def _resolve_under(root: str, p: str) -> str:
    rel = _normalize_rel_path(p)
    abs_p = os.path.abspath(os.path.join(root, rel))
    root_abs = os.path.abspath(root)
    if abs_p != root_abs and not abs_p.startswith(root_abs + os.sep):
        raise ValueError("path escapes root")
    return abs_p


def _outputs_from_openclaw_result(result: Dict[str, Json], work_dir: str) -> List[str]:
    outs: List[str] = []
    if not isinstance(result, dict):
        return outs
    tool_calls = result.get("toolCalls")
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            if tc.get("tool") != "write_file":
                continue
            out = tc.get("output")
            if not isinstance(out, dict):
                continue
            p = out.get("path")
            if not isinstance(p, str) or not p:
                continue
            try:
                abs_p = _resolve_under(work_dir, p)
            except Exception:
                continue
            if os.path.exists(abs_p):
                outs.append(os.path.abspath(abs_p))
    paths = result.get("paths")
    if isinstance(paths, list):
        for p in paths:
            if not isinstance(p, str) or not p:
                continue
            try:
                abs_p = _resolve_under(work_dir, p)
            except Exception:
                continue
            if os.path.exists(abs_p):
                outs.append(os.path.abspath(abs_p))
    return sorted(set(outs))


def _openclaw_default_agent_id(env: Optional[Dict[str, str]] = None) -> Optional[str]:
    try:
        p = subprocess.run(
            ["openclaw", "agents", "list", "--json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=(env or os.environ.copy()),
        )
    except Exception:
        return None
    obj = _json_first_value(p.stdout or "")
    if isinstance(obj, dict):
        agents = obj.get("agents")
        if isinstance(agents, list):
            for a in agents:
                if isinstance(a, dict) and isinstance(a.get("id"), str) and a.get("id"):
                    return str(a.get("id"))
    if isinstance(obj, list):
        for a in obj:
            if isinstance(a, dict) and isinstance(a.get("id"), str) and a.get("id"):
                return str(a.get("id"))
    return None


def _copy_session_jsonl(*, state_dir: str, agent_id: str, session_id: str, dst_dir: str) -> Optional[str]:
    src = os.path.join(os.path.abspath(state_dir), "agents", agent_id, "sessions", f"{session_id}.jsonl")
    if not os.path.exists(src) or not os.path.isfile(src):
        return None
    dst = os.path.join(dst_dir, "session.jsonl")
    try:
        shutil.copy2(src, dst)
        return dst
    except Exception:
        return None


def _safe_json_loads(line: str) -> Optional[Dict[str, Json]]:
    try:
        obj = json.loads(line)
    except Exception:
        return None
    if isinstance(obj, dict):
        return obj
    return None


def _capture_openclaw_preflight(*, env: Dict[str, str], cwd: str, out_path: str) -> None:
    commands = {
        "config_file": ["openclaw", "config", "file"],
        "default_model": ["openclaw", "config", "get", "agents.defaults.model"],
        "agents_list": ["openclaw", "agents", "list", "--json"],
    }
    result: Dict[str, Json] = {
        "cwd": os.path.abspath(cwd),
        "stateDir": env.get("OPENCLAW_STATE_DIR"),
        "configPath": env.get("OPENCLAW_CONFIG_PATH"),
        "envModel": env.get("OPENAI_MODEL"),
        "envBaseUrl": env.get("OPENAI_BASE_URL"),
        "commands": {},
    }
    for name, cmd in commands.items():
        try:
            p = subprocess.run(
                cmd,
                cwd=os.path.abspath(cwd),
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            result["commands"][name] = {
                "cmd": cmd,
                "exitCode": int(p.returncode),
                "stdout": (p.stdout or "")[:4000],
                "stderr": (p.stderr or "")[:4000],
            }
        except Exception as e:
            result["commands"][name] = {
                "cmd": cmd,
                "error": str(e),
            }
    _write_json(out_path, result)


def _build_openclaw_temp_config(
    *,
    dst_path: str,
    provider_id: str,
    target_base_url: str,
    target_model: str,
    target_api_key: Optional[str],
    workspace_dir: str,
) -> str:
    src_path = os.environ.get("OPENCLAW_CONFIG_PATH") or os.path.expanduser("~/.openclaw/openclaw.json")
    cfg: Dict[str, Json] = {}
    try:
        if os.path.exists(src_path) and os.path.isfile(src_path):
            loaded = _read_json(src_path)
            if isinstance(loaded, dict):
                cfg = loaded
    except Exception:
        cfg = {}

    models = cfg.get("models")
    if not isinstance(models, dict):
        models = {}
    providers = models.get("providers")
    if not isinstance(providers, dict):
        providers = {}
    provider_cfg = providers.get(provider_id)
    if not isinstance(provider_cfg, dict):
        provider_cfg = {}
    provider_cfg["baseUrl"] = target_base_url
    provider_cfg["api"] = "openai-completions"
    if isinstance(target_api_key, str) and target_api_key:
        provider_cfg["apiKey"] = target_api_key
    provider_cfg["models"] = [
        {
            "id": target_model,
            "name": target_model,
            "api": "openai-completions",
        }
    ]
    providers[provider_id] = provider_cfg
    models["providers"] = providers
    cfg["models"] = models

    agents = cfg.get("agents")
    if not isinstance(agents, dict):
        agents = {}
    defaults = agents.get("defaults")
    if not isinstance(defaults, dict):
        defaults = {}
    model_cfg = defaults.get("model")
    if not isinstance(model_cfg, dict):
        model_cfg = {}
    model_ref = f"{provider_id}/{target_model}"
    model_cfg["primary"] = model_ref
    defaults["model"] = model_cfg
    defaults["workspace"] = os.path.abspath(workspace_dir)
    allowed_models = defaults.get("models")
    if not isinstance(allowed_models, dict):
        allowed_models = {}
    alias_entry = allowed_models.get(model_ref)
    if not isinstance(alias_entry, dict):
        alias_entry = {}
    alias_entry["alias"] = target_model
    allowed_models[model_ref] = alias_entry
    defaults["models"] = allowed_models
    agents["defaults"] = defaults
    cfg["agents"] = agents

    _write_json(dst_path, cfg)
    return dst_path


def _find_latest_session(*, state_dir: str, agent_id: str) -> Tuple[Optional[str], Optional[str]]:
    sessions_dir = os.path.join(os.path.abspath(state_dir), "agents", str(agent_id), "sessions")
    sessions_json = os.path.join(sessions_dir, "sessions.json")
    if os.path.exists(sessions_json) and os.path.isfile(sessions_json):
        try:
            obj = _read_json(sessions_json)
            if isinstance(obj, dict):
                best = None
                best_ts = None
                for _, v in obj.items():
                    if not isinstance(v, dict):
                        continue
                    sid = v.get("sessionId")
                    ts = v.get("updatedAt")
                    if not isinstance(sid, str) or not sid:
                        continue
                    if not isinstance(ts, (int, float)):
                        continue
                    if best is None or ts > (best_ts or 0):
                        best = sid
                        best_ts = ts
                if best:
                    cand = os.path.join(sessions_dir, f"{best}.jsonl")
                    if os.path.exists(cand) and os.path.isfile(cand):
                        return best, cand
        except Exception:
            pass
    try:
        best_path = None
        best_m = None
        for fn in os.listdir(sessions_dir) if os.path.isdir(sessions_dir) else []:
            if not fn.endswith(".jsonl"):
                continue
            if fn == "sessions.jsonl":
                continue
            p = os.path.join(sessions_dir, fn)
            if not os.path.isfile(p):
                continue
            try:
                mt = os.path.getmtime(p)
            except Exception:
                continue
            if best_path is None or mt > (best_m or 0):
                best_path = p
                best_m = mt
        if best_path:
            sid = os.path.splitext(os.path.basename(best_path))[0]
            return sid, best_path
    except Exception:
        pass
    return None, None


def _normalize_openai_usage(obj: Json) -> Optional[Dict[str, int]]:
    if not isinstance(obj, dict):
        return None
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens")
    if not isinstance(prompt_tokens, int):
        prompt_tokens = 0
    if not isinstance(completion_tokens, int):
        completion_tokens = 0
    if not isinstance(total_tokens, int):
        total_tokens = prompt_tokens + completion_tokens
    prompt_details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    completion_details = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
    return {
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "total_tokens": int(total_tokens),
        "cache_read": int(prompt_details.get("cached_tokens") or 0),
        "cache_write": int(completion_details.get("reasoning_tokens") or 0),
    }


def _start_openclaw_usage_proxy(*, target_base_url: str, log_path: str) -> Tuple[socketserver.BaseServer, threading.Thread, str]:
    target_root = str(target_base_url or "").rstrip("/")
    records: List[Dict[str, Json]] = []
    lock = threading.Lock()

    class _ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True
        daemon_threads = True

    class _Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            return

        def _proxy(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length > 0 else b""
            target_url = target_root + self.path
            headers = {}
            for k, v in self.headers.items():
                kl = str(k).lower()
                if kl in {"host", "content-length", "connection"}:
                    continue
                headers[k] = v
            req = urllib.request.Request(target_url, data=body, headers=headers, method=self.command)
            started = time.time()
            status_code = 502
            response_headers: Dict[str, str] = {}
            response_body = b""
            error_text = None
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    status_code = int(resp.getcode() or 200)
                    response_headers = dict(resp.headers.items())
                    response_body = resp.read()
            except urllib.error.HTTPError as e:
                status_code = int(getattr(e, "code", 502) or 502)
                response_headers = dict(e.headers.items()) if getattr(e, "headers", None) is not None else {}
                try:
                    response_body = e.read()
                except Exception:
                    response_body = b""
                error_text = f"HTTPError {status_code}"
            except Exception as e:
                response_body = str(e).encode("utf-8", errors="ignore")
                error_text = str(e)

            usage = None
            response_text = None
            try:
                response_text = response_body.decode("utf-8", errors="ignore")
                response_obj = _json_first_value(response_text)
                usage = _normalize_openai_usage(response_obj)
            except Exception:
                response_text = None

            with lock:
                records.append(
                    {
                        "timestamp": time.time(),
                        "method": self.command,
                        "path": self.path,
                        "targetUrl": target_url,
                        "statusCode": status_code,
                        "durationMs": int((time.time() - started) * 1000),
                        "usage": usage,
                        "error": error_text,
                        "requestBodyHead": body[:2000].decode("utf-8", errors="ignore"),
                        "responseBodyHead": (response_text[:2000] if isinstance(response_text, str) else None),
                    }
                )
                _write_json(log_path, records)

            self.send_response(status_code)
            for k, v in response_headers.items():
                kl = str(k).lower()
                if kl in {"transfer-encoding", "connection", "content-length"}:
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            if response_body:
                self.wfile.write(response_body)

        def do_POST(self) -> None:
            self._proxy()

        def do_GET(self) -> None:
            self._proxy()

        def do_OPTIONS(self) -> None:
            self._proxy()

    server = _ThreadingTCPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}"


def _patch_openclaw_models_file(
    *,
    models_path: str,
    proxy_url: str,
    target_model: Optional[str],
    target_api_key: Optional[str],
) -> Optional[str]:
    obj = _read_json(models_path)
    if not isinstance(obj, dict):
        return None
    providers = obj.get("providers")
    if not isinstance(providers, dict):
        return None
    patched: List[str] = []
    for name, cfg in providers.items():
        if not isinstance(cfg, dict):
            continue
        if cfg.get("api") != "openai-completions":
            continue
        cfg["baseUrl"] = proxy_url
        if isinstance(target_api_key, str) and target_api_key:
            cfg["apiKey"] = target_api_key
        models = cfg.get("models") if isinstance(cfg.get("models"), list) else []
        if isinstance(target_model, str) and target_model:
            for m in models:
                if not isinstance(m, dict):
                    continue
                if isinstance(m.get("id"), str) and m.get("id"):
                    m["id"] = target_model
                if isinstance(m.get("name"), str) and m.get("name"):
                    m["name"] = target_model
        patched.append(str(name))
    if not patched:
        return None
    _write_json(models_path, obj)
    return models_path


def _enable_openclaw_usage_proxy(
    *,
    state_dir: str,
    agent_id: str,
    target_base_url: str,
    target_model: Optional[str],
    target_api_key: Optional[str],
    proxy_url: str,
) -> Optional[str]:
    safe_agent_id = str(agent_id or "").strip() or "main"
    src_models = os.path.expanduser(os.path.join("~", ".openclaw", "agents", safe_agent_id, "agent", "models.json"))
    if not os.path.exists(src_models):
        src_models = os.path.expanduser("~/.openclaw/agents/main/agent/models.json")
    dst_models = os.path.join(os.path.abspath(state_dir), "agents", safe_agent_id, "agent", "models.json")
    if not os.path.exists(dst_models):
        if not os.path.exists(src_models):
            return None
        _ensure_dir(os.path.dirname(dst_models))
        shutil.copy2(src_models, dst_models)
    return _patch_openclaw_models_file(
        models_path=dst_models,
        proxy_url=proxy_url,
        target_model=target_model,
        target_api_key=target_api_key,
    )


def _merge_proxy_usage_into_trace(trace_core: Dict[str, Json], proxy_log_path: Optional[str]) -> Dict[str, Json]:
    if not proxy_log_path or not os.path.exists(proxy_log_path):
        return trace_core
    try:
        proxy_rows = _read_json(proxy_log_path)
    except Exception:
        return trace_core
    if not isinstance(proxy_rows, list):
        return trace_core
    proxy_usage_rows: List[Dict[str, int]] = []
    for row in proxy_rows:
        if not isinstance(row, dict):
            continue
        if row.get("statusCode") and int(row.get("statusCode") or 0) >= 400:
            continue
        usage = row.get("usage")
        if isinstance(usage, dict):
            proxy_usage_rows.append(
                {
                    "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                    "completion_tokens": int(usage.get("completion_tokens") or 0),
                    "total_tokens": int(usage.get("total_tokens") or 0),
                    "cache_read": int(usage.get("cache_read") or 0),
                    "cache_write": int(usage.get("cache_write") or 0),
                }
            )
    execution_trace = trace_core.get("executionTrace")
    if not isinstance(execution_trace, list):
        execution_trace = []
    assistant_events: List[Dict[str, Json]] = []
    for ev in execution_trace:
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "text" and ev.get("role") == "assistant" and isinstance(ev.get("llm"), dict):
            assistant_events.append(ev)
    usage_idx = 0
    for ev in assistant_events:
        llm = ev.get("llm") if isinstance(ev.get("llm"), dict) else {}
        existing = llm.get("usage") if isinstance(llm.get("usage"), dict) else {}
        existing_total = int(existing.get("total_tokens") or 0) if isinstance(existing, dict) else 0
        if existing_total > 0:
            continue
        if usage_idx >= len(proxy_usage_rows):
            break
        llm["usage"] = proxy_usage_rows[usage_idx]
        llm["usageSource"] = "proxy"
        ev["llm"] = llm
        usage_idx += 1
    merged_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cache_read": 0, "cache_write": 0}
    for ev in assistant_events:
        llm = ev.get("llm") if isinstance(ev.get("llm"), dict) else {}
        usage = llm.get("usage") if isinstance(llm.get("usage"), dict) else {}
        merged_total["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        merged_total["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        merged_total["total_tokens"] += int(usage.get("total_tokens") or 0)
        merged_total["cache_read"] += int(usage.get("cache_read") or 0)
        merged_total["cache_write"] += int(usage.get("cache_write") or 0)
    trace_core["executionTrace"] = execution_trace
    trace_core["usageTotal"] = merged_total
    trace_core["proxyUsage"] = proxy_usage_rows
    trace_core["proxyLogPath"] = proxy_log_path
    return trace_core


def _write_fetch_hook(path: str, log_path: str) -> None:
    js = f"""import fs from 'node:fs';

const LOG_PATH = {json.dumps(log_path)};
const origFetch = globalThis.fetch;
const MAX_ATTEMPTS = 20;
const BASE_BACKOFF_MS = 1000;
const MAX_BACKOFF_MS = 30000;

function normUsage(usage) {{
  if (!usage || typeof usage !== 'object') return null;
  const prompt = Number(usage.prompt_tokens ?? usage.input ?? 0) || 0;
  const completion = Number(usage.completion_tokens ?? usage.output ?? 0) || 0;
  const total = Number(usage.total_tokens ?? usage.totalTokens ?? (prompt + completion)) || (prompt + completion);
  const promptDetails = usage.prompt_tokens_details && typeof usage.prompt_tokens_details === 'object' ? usage.prompt_tokens_details : {{}};
  const completionDetails = usage.completion_tokens_details && typeof usage.completion_tokens_details === 'object' ? usage.completion_tokens_details : {{}};
  return {{
    prompt_tokens: prompt,
    completion_tokens: completion,
    total_tokens: total,
    cache_read: Number(promptDetails.cached_tokens ?? usage.cacheRead ?? 0) || 0,
    cache_write: Number(completionDetails.reasoning_tokens ?? usage.cacheWrite ?? 0) || 0,
  }};
}}

function extractUsage(text) {{
  if (!text) return null;
  try {{
    const obj = JSON.parse(text);
    const usage = normUsage(obj?.usage);
    if (usage) return usage;
  }} catch {{}}
  const lines = String(text).split(/\\r?\\n/);
  for (let i = lines.length - 1; i >= 0; i -= 1) {{
    const line = lines[i].trim();
    if (!line.startsWith('data:')) continue;
    const payload = line.slice(5).trim();
    if (!payload || payload === '[DONE]') continue;
    try {{
      const obj = JSON.parse(payload);
      const usage = normUsage(obj?.usage);
      if (usage) return usage;
    }} catch {{}}
  }}
  return null;
}}

function appendLog(entry) {{
  try {{
    fs.appendFileSync(LOG_PATH, JSON.stringify(entry) + '\\n', 'utf8');
  }} catch {{}}
}}

function shouldRetryStatus(status) {{
  return status === 408 || status === 409 || status === 425 || status === 429 || status === 500 || status === 502 || status === 503 || status === 504;
}}

function computeBackoffMs(attempt) {{
  const exp = Math.max(0, attempt - 1);
  const base = Math.min(MAX_BACKOFF_MS, BASE_BACKOFF_MS * Math.pow(2, exp));
  const jitter = Math.floor(Math.random() * 250);
  return base + jitter;
}}

function sleep(ms) {{
  return new Promise((resolve) => setTimeout(resolve, ms));
}}

if (typeof origFetch === 'function') {{
  globalThis.fetch = async function patchedFetch(input, init) {{
    const requestUrl = typeof input === 'string' ? input : (input && typeof input.url === 'string' ? input.url : '');
    const isChat = requestUrl.includes('/chat/completions');
    let nextInit = init;
    let requestBodyHead = null;
    if (isChat && init && typeof init === 'object' && typeof init.body === 'string') {{
      requestBodyHead = init.body.slice(0, 2000);
      try {{
        const body = JSON.parse(init.body);
        if (body && typeof body === 'object' && body.stream === true) {{
          const streamOptions = body.stream_options && typeof body.stream_options === 'object' ? body.stream_options : {{}};
          body.stream_options = {{ ...streamOptions, include_usage: true }};
          nextInit = {{ ...init, body: JSON.stringify(body) }};
          requestBodyHead = nextInit.body.slice(0, 2000);
        }}
      }} catch {{}}
    }}
    const maxAttempts = isChat ? MAX_ATTEMPTS : 1;
    let lastError = null;
    for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {{
      const started = Date.now();
      try {{
        const res = await origFetch.call(this, input, nextInit);
        if (!isChat) return res;
        const cloned = res.clone();
        let text = '';
        try {{
          text = await cloned.text();
        }} catch (err) {{
          appendLog({{
            timestamp: Date.now() / 1000,
            path: requestUrl,
            statusCode: res.status,
            durationMs: Date.now() - started,
            usage: null,
            requestBodyHead,
            attempt,
            maxAttempts,
            error: String(err),
          }});
          return res;
        }}
        const usage = extractUsage(text);
        const retryable = shouldRetryStatus(res.status) && attempt < maxAttempts;
        appendLog({{
          timestamp: Date.now() / 1000,
          path: requestUrl,
          statusCode: res.status,
          durationMs: Date.now() - started,
          usage,
          requestBodyHead,
          responseBodyHead: String(text || '').slice(0, 2000),
          attempt,
          maxAttempts,
          willRetry: retryable,
        }});
        if (!retryable) return res;
        await sleep(computeBackoffMs(attempt));
      }} catch (err) {{
        lastError = err;
        if (isChat) {{
          const retryable = attempt < maxAttempts;
          appendLog({{
            timestamp: Date.now() / 1000,
            path: requestUrl,
            statusCode: null,
            durationMs: Date.now() - started,
            usage: null,
            requestBodyHead,
            attempt,
            maxAttempts,
            willRetry: retryable,
            error: String(err),
          }});
          if (retryable) {{
            await sleep(computeBackoffMs(attempt));
            continue;
          }}
        }}
        throw err;
      }}
    }}
    throw lastError ?? new Error('chat/completions retry attempts exhausted');
  }};
}}
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(js)


def _merge_fetch_log_usage_into_trace(trace_core: Dict[str, Json], fetch_log_path: Optional[str]) -> Dict[str, Json]:
    if not fetch_log_path or not os.path.exists(fetch_log_path):
        return trace_core
    rows: List[Dict[str, Json]] = []
    try:
        with open(fetch_log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = _safe_json_loads(line)
                if isinstance(obj, dict):
                    rows.append(obj)
    except Exception:
        return trace_core
    if not rows:
        return trace_core
    usage_rows: List[Dict[str, int]] = []
    for row in rows:
        usage = row.get("usage")
        if not isinstance(usage, dict):
            continue
        usage_rows.append(
            {
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
                "cache_read": int(usage.get("cache_read") or 0),
                "cache_write": int(usage.get("cache_write") or 0),
            }
        )
    if not usage_rows:
        return trace_core
    execution_trace = trace_core.get("executionTrace")
    if not isinstance(execution_trace, list):
        return trace_core
    idx = 0
    for ev in execution_trace:
        if not isinstance(ev, dict):
            continue
        if ev.get("type") != "text" or ev.get("role") != "assistant":
            continue
        llm = ev.get("llm") if isinstance(ev.get("llm"), dict) else {}
        usage = llm.get("usage") if isinstance(llm.get("usage"), dict) else {}
        if int(usage.get("total_tokens") or 0) > 0:
            continue
        if idx >= len(usage_rows):
            break
        llm["usage"] = usage_rows[idx]
        llm["usageSource"] = "fetch_hook"
        ev["llm"] = llm
        idx += 1
    total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cache_read": 0, "cache_write": 0}
    for ev in execution_trace:
        if not isinstance(ev, dict):
            continue
        llm = ev.get("llm") if isinstance(ev.get("llm"), dict) else {}
        usage = llm.get("usage") if isinstance(llm.get("usage"), dict) else {}
        total["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        total["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        total["total_tokens"] += int(usage.get("total_tokens") or 0)
        total["cache_read"] += int(usage.get("cache_read") or 0)
        total["cache_write"] += int(usage.get("cache_write") or 0)
    trace_core["usageTotal"] = total
    trace_core["fetchLogPath"] = fetch_log_path
    trace_core["fetchHookUsage"] = usage_rows
    return trace_core


def _extract_openclaw_trace(*, session_jsonl_path: str, base_url: Optional[str], model: Optional[str]) -> Dict[str, Json]:
    execution_trace: List[Json] = []
    tool_index: Dict[str, Dict[str, Json]] = {}
    turns = 0
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    last_text = ""
    provider = None

    def add_usage_total(u: Dict[str, Json]) -> Dict[str, Json]:
        pt = int(u.get("input") or 0) if isinstance(u, dict) else 0
        ct = int(u.get("output") or 0) if isinstance(u, dict) else 0
        tt = int(u.get("totalTokens") or 0) if isinstance(u, dict) else 0
        usage_total["prompt_tokens"] += pt
        usage_total["completion_tokens"] += ct
        usage_total["total_tokens"] += (tt or (pt + ct))
        return {
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": tt or (pt + ct),
            "cache_read": int(u.get("cacheRead") or 0) if isinstance(u, dict) else 0,
            "cache_write": int(u.get("cacheWrite") or 0) if isinstance(u, dict) else 0,
        }

    with open(session_jsonl_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            evt = _safe_json_loads(line)
            if not isinstance(evt, dict):
                continue
            if evt.get("type") != "message":
                continue
            msg = evt.get("message")
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            ts = evt.get("timestamp") if isinstance(evt.get("timestamp"), str) else None

            if role == "user":
                content = msg.get("content")
                if isinstance(content, list):
                    txts = []
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str):
                            txts.append(str(c.get("text")))
                    execution_trace.append(
                        {
                            "type": "text",
                            "role": "user",
                            "content": "\n".join(txts),
                            "timestamp": ts,
                        }
                    )
                continue

            if role == "assistant":
                turns += 1
                if isinstance(msg.get("provider"), str) and msg.get("provider"):
                    provider = str(msg.get("provider"))
                if isinstance(msg.get("model"), str) and msg.get("model") and not model:
                    model = str(msg.get("model"))
                content = msg.get("content")
                text_parts: List[str] = []
                if isinstance(content, list):
                    for c in content:
                        if not isinstance(c, dict):
                            continue
                        if c.get("type") == "text" and isinstance(c.get("text"), str):
                            text_parts.append(str(c.get("text")))
                        elif c.get("type") == "toolCall":
                            tcid = c.get("id")
                            nm = c.get("name")
                            args = c.get("arguments") if isinstance(c.get("arguments"), dict) else {}
                            if isinstance(tcid, str) and tcid and isinstance(nm, str) and nm:
                                ev = {
                                    "type": "tool",
                                    "role": "tool",
                                    "tool": nm,
                                    "callID": tcid,
                                    "timestamp": ts,
                                    "startedAt": ts,
                                    "finishedAt": None,
                                    "durationMs": None,
                                    "input": args,
                                    "output": None,
                                    "exitCode": None,
                                }
                                execution_trace.append(ev)
                                tool_index[tcid] = ev
                text = "\n".join([t for t in text_parts if t]).strip()
                if text:
                    last_text = text
                u = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
                usage = add_usage_total(u) if isinstance(u, dict) else {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cache_read": 0, "cache_write": 0}
                execution_trace.append(
                    {
                        "type": "text",
                        "role": "assistant",
                        "content": text,
                        "timestamp": ts,
                        "turn": turns,
                        "llm": {
                            "provider": provider,
                            "baseUrl": base_url,
                            "model": model,
                            "usage": usage,
                            "stopReason": msg.get("stopReason") if isinstance(msg.get("stopReason"), str) else None,
                            "errorMessage": msg.get("errorMessage") if isinstance(msg.get("errorMessage"), str) else None,
                        },
                    }
                )
                continue

            if role == "toolResult":
                tcid = msg.get("toolCallId") if isinstance(msg.get("toolCallId"), str) else None
                tool_name = msg.get("toolName") if isinstance(msg.get("toolName"), str) else None
                content = msg.get("content")
                text = ""
                if isinstance(content, list):
                    txts = []
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str):
                            txts.append(str(c.get("text")))
                    text = "\n".join(txts)
                det = msg.get("details") if isinstance(msg.get("details"), dict) else {}
                if tcid and tcid in tool_index:
                    t0 = tool_index[tcid]
                    t0["finishedAt"] = ts
                    if isinstance(det.get("durationMs"), int):
                        t0["durationMs"] = int(det.get("durationMs") or 0)
                    if isinstance(det.get("exitCode"), int):
                        t0["exitCode"] = int(det.get("exitCode") or 0)
                        t0["output"] = {"text": text}
                    else:
                        t0["output"] = {"text": text}
                    if isinstance(det.get("status"), str):
                        t0["status"] = str(det.get("status"))
                continue

    usage_total["total_tokens"] = usage_total["total_tokens"] or (usage_total["prompt_tokens"] + usage_total["completion_tokens"])
    return {
        "turns": turns,
        "lastText": last_text,
        "executionTrace": execution_trace,
        "usageTotal": usage_total,
        "llm": {"provider": provider, "baseUrl": base_url, "model": model},
    }


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

    prompt = str(prompt or "").strip()
    if not prompt:
        return {"status": "error", "paths": [], "errorMessage": "Missing prompt"}

    env = os.environ.copy()
    state_dir = os.path.join(raw_dir, "openclaw_state")
    case_home = os.path.join(raw_dir, "openclaw_home")
    _ensure_dir(state_dir)
    _ensure_dir(case_home)
    _ensure_dir(os.path.join(case_home, ".openclaw"))
    env["OPENCLAW_STATE_DIR"] = state_dir
    env["HOME"] = case_home
    env["OPENCLAW_HOME"] = case_home
    config_path = os.path.join(raw_dir, "openclaw.json")
    provider_id = str(api_provider.get("provider_type") or "openai") if isinstance(api_provider, dict) else "openai"
    fetch_hook_path = os.path.join(raw_dir, "openclaw_fetch_hook.mjs")
    fetch_log_path = os.path.join(raw_dir, "openclaw_fetch_log.jsonl")

    base_url = api_provider.get("baseUrl") if isinstance(api_provider, dict) else None
    model = api_provider.get("model") if isinstance(api_provider, dict) else None
    api_key = api_provider.get("apiKey") if isinstance(api_provider, dict) else None
    proxy_server = None
    proxy_thread = None
    proxy_url = None
    proxy_log_path = os.path.join(raw_dir, "openclaw_proxy_log.json")
    patched_models_path = None
    resolved_agent_id = str(agent_id or "").strip() or env.get("OPENCLAW_AGENT_ID") or "main"
    if not resolved_agent_id:
        return {"status": "error", "paths": [], "errorMessage": "Cannot resolve openclaw agent id"}
    if isinstance(base_url, str) and base_url.strip():
        try:
            proxy_server, proxy_thread, proxy_url = _start_openclaw_usage_proxy(
                target_base_url=base_url.strip(),
                log_path=proxy_log_path,
            )
            patched_models_path = _enable_openclaw_usage_proxy(
                state_dir=state_dir,
                agent_id=str(resolved_agent_id),
                target_base_url=base_url.strip(),
                target_model=(model.strip() if isinstance(model, str) and model.strip() else None),
                target_api_key=(api_key.strip() if isinstance(api_key, str) and api_key.strip() else None),
                proxy_url=proxy_url,
            )
        except Exception:
            proxy_server = None
            proxy_thread = None
            proxy_url = None
    try:
        _write_fetch_hook(fetch_hook_path, fetch_log_path)
        existing_node_options = env.get("NODE_OPTIONS", "").strip()
        hook_opt = f"--import={fetch_hook_path}"
        env["NODE_OPTIONS"] = f"{existing_node_options} {hook_opt}".strip() if existing_node_options else hook_opt
    except Exception:
        fetch_hook_path = ""
        fetch_log_path = ""
    if isinstance(base_url, str) and base_url.strip():
        env["OPENAI_BASE_URL"] = proxy_url or base_url.strip()
    if isinstance(model, str) and model.strip():
        env["OPENAI_MODEL"] = model.strip()
    if isinstance(api_key, str) and api_key.strip():
        env["OPENAI_API_KEY"] = api_key.strip()

    cmd = ["openclaw", "agent", "--local", "--json", "--message", prompt, "--agent", str(resolved_agent_id)]
    if isinstance(timeout_s, (int, float)) and timeout_s > 0:
        cmd.extend(["--timeout", str(int(timeout_s))])

    if isinstance(base_url, str) and base_url.strip() and isinstance(model, str) and model.strip():
        try:
            env["OPENCLAW_CONFIG_PATH"] = _build_openclaw_temp_config(
                dst_path=config_path,
                provider_id=provider_id,
                target_base_url=(proxy_url or base_url.strip()),
                target_model=model.strip(),
                target_api_key=(api_key.strip() if isinstance(api_key, str) and api_key.strip() else None),
                workspace_dir=os.path.abspath(work_dir),
            )
        except Exception:
            env["OPENCLAW_CONFIG_PATH"] = config_path

    #region debug-point openclaw_preflight
    try:
        _capture_openclaw_preflight(
            env=env,
            cwd=os.path.abspath(work_dir),
            out_path=os.path.join(raw_dir, "openclaw_preflight.json"),
        )
    except Exception:
        pass
    #endregion debug-point openclaw_preflight

    used_timeout = timeout_s if isinstance(timeout_s, (int, float)) and timeout_s > 0 else None
    try:
        try:
            p = subprocess.run(
                ["openclaw", "--no-color", "--log-level", "silent", *cmd[1:]],
                cwd=os.path.abspath(work_dir),
                env=env,
                capture_output=True,
                text=True,
                timeout=used_timeout,
            )
            exit_code = int(p.returncode)
            stdout_text = p.stdout or ""
            stderr_text = p.stderr or ""
        except subprocess.TimeoutExpired as e:
            exit_code = 124
            stdout_text = e.stdout or ""
            stderr_text = e.stderr or ""
        except Exception as e:
            exit_code = 1
            stdout_text = ""
            stderr_text = str(e)
    finally:
        if proxy_server is not None:
            try:
                proxy_server.shutdown()
            except Exception:
                pass
            try:
                proxy_server.server_close()
            except Exception:
                pass
        if proxy_thread is not None:
            try:
                proxy_thread.join(timeout=2)
            except Exception:
                pass

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

    _write_json(
        os.path.join(raw_dir, "openclaw_invocation.json"),
        {
            "cmd": cmd,
            "cwd": os.path.abspath(work_dir),
            "workspaceDir": os.path.abspath(work_dir),
            "homeDir": case_home,
            "stateDir": state_dir,
            "agentId": str(resolved_agent_id),
            "exitCode": exit_code,
            "startedAt": started_at,
            "finishedAt": time.time(),
            "proxyUrl": proxy_url,
            "proxyLogPath": proxy_log_path if os.path.exists(proxy_log_path) else None,
            "patchedModelsPath": patched_models_path,
        },
    )
    with open(os.path.join(raw_dir, "stdout.txt"), "w", encoding="utf-8") as f:
        f.write(stdout_text)
    with open(os.path.join(raw_dir, "stderr.txt"), "w", encoding="utf-8") as f:
        f.write(stderr_text)

    parsed = _json_first_value(stdout_text)
    if isinstance(parsed, (dict, list)):
        _write_json(os.path.join(raw_dir, "result.parsed.json"), parsed)

    session_id = None
    if isinstance(parsed, dict):
        meta = parsed.get("meta")
        if isinstance(meta, dict):
            am = meta.get("agentMeta")
            if isinstance(am, dict) and isinstance(am.get("sessionId"), str) and am.get("sessionId"):
                session_id = str(am.get("sessionId"))
    session_jsonl = None
    if session_id:
        session_jsonl = _copy_session_jsonl(state_dir=state_dir, agent_id=str(resolved_agent_id), session_id=session_id, dst_dir=raw_dir)
    if not session_jsonl:
        sid2, src2 = _find_latest_session(state_dir=state_dir, agent_id=str(resolved_agent_id))
        if sid2 and not session_id:
            session_id = sid2
        if src2:
            try:
                shutil.copy2(src2, os.path.join(raw_dir, "session.jsonl"))
                session_jsonl = os.path.join(raw_dir, "session.jsonl")
            except Exception:
                session_jsonl = None

    outs = _outputs_from_openclaw_result(parsed if isinstance(parsed, dict) else {}, os.path.abspath(work_dir))
    last_text = ""
    if isinstance(parsed, dict):
        tos = parsed.get("textOutputs")
        if isinstance(tos, list) and tos and isinstance(tos[-1], str):
            last_text = tos[-1]
        elif isinstance(parsed.get("reply"), str):
            last_text = str(parsed.get("reply"))
        elif isinstance(parsed.get("text"), str):
            last_text = str(parsed.get("text"))

    status = "ok"
    if exit_code == 124:
        status = "timeout"
    elif exit_code != 0:
        status = "error"

    duration_ms = int((time.time() - started_at) * 1000)
    trace_core: Dict[str, Json] = {"executionTrace": [], "usageTotal": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, "llm": {"provider": None, "baseUrl": base_url, "model": model}}
    if session_jsonl and os.path.exists(session_jsonl) and os.path.isfile(session_jsonl):
        try:
            trace_core = _extract_openclaw_trace(session_jsonl_path=session_jsonl, base_url=str(base_url) if isinstance(base_url, str) else None, model=str(model) if isinstance(model, str) else None)
        except Exception:
            trace_core = trace_core
    trace_core = _merge_proxy_usage_into_trace(trace_core, proxy_log_path if os.path.exists(proxy_log_path) else None)
    trace_core = _merge_fetch_log_usage_into_trace(trace_core, fetch_log_path if fetch_log_path and os.path.exists(fetch_log_path) else None)
    if isinstance(trace_core.get("lastText"), str) and trace_core.get("lastText"):
        last_text = str(trace_core.get("lastText"))

    return {
        "status": status,
        "paths": outs,
        "errorMessage": (
            (f"Timeout after {timeout_s}s" if status == "timeout" else (stderr_text[:2000] if status == "error" and isinstance(stderr_text, str) else None))
        ),
        "trace": {
            "runner": "openclaw",
            "agentId": str(resolved_agent_id),
            "sessionId": session_id,
            "sessionJsonlPath": session_jsonl,
            "rawDir": raw_dir,
            "lastText": last_text,
            "executionTrace": trace_core.get("executionTrace") if isinstance(trace_core.get("executionTrace"), list) else [],
            "llm": trace_core.get("llm") if isinstance(trace_core.get("llm"), dict) else {},
            "usageTotal": trace_core.get("usageTotal") if isinstance(trace_core.get("usageTotal"), dict) else {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "proxyLogPath": trace_core.get("proxyLogPath") if isinstance(trace_core.get("proxyLogPath"), str) else None,
            "proxyCapturedCalls": len(trace_core.get("proxyUsage")) if isinstance(trace_core.get("proxyUsage"), list) else 0,
            "fetchLogPath": trace_core.get("fetchLogPath") if isinstance(trace_core.get("fetchLogPath"), str) else None,
            "fetchCapturedCalls": len(trace_core.get("fetchHookUsage")) if isinstance(trace_core.get("fetchHookUsage"), list) else 0,
        },
        "metrics": {
            "turns": trace_core.get("turns") if isinstance(trace_core.get("turns"), int) else None,
            "promptTokens": (trace_core.get("usageTotal").get("prompt_tokens") if isinstance(trace_core.get("usageTotal"), dict) else None),
            "completionTokens": (trace_core.get("usageTotal").get("completion_tokens") if isinstance(trace_core.get("usageTotal"), dict) else None),
            "totalTokens": (trace_core.get("usageTotal").get("total_tokens") if isinstance(trace_core.get("usageTotal"), dict) else None),
        },
        "durationMs": duration_ms,
    }
