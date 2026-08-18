from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hashlib
from importlib import metadata as importlib_metadata
import json
import os
import re
import time
from typing import Any, Dict, Iterable, List, Optional


Json = Any

DEEPSEEK_HARNESS_DISTRIBUTION = "deepseek-harness-sdk"
DEEPSEEK_HARNESS_SDK_VERSION = "0.1.0rc7"
DEEPSEEK_HARNESS_PROVIDER = "deepseek-official"
DEEPSEEK_HARNESS_PROFILE = "jsonrpc-agent-minimal-99f6f02"
DEEPSEEK_HARNESS_CORDIS_SHA256 = (
    "4ddf99b5492fac7b578e3caddb0158815e44d5db176ba0aeab57012d35299fca"
)
DEEPSEEK_HARNESS_CORDIS_PATH = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "vendor",
        "deepseek-harness",
        "minimal.cordis.yml",
    )
)


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _jsonable(value: Json) -> Json:
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return _jsonable(value.model_dump())
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _write_json(path: str, value: Json) -> None:
    _ensure_dir(os.path.dirname(os.path.abspath(path)))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(_jsonable(value), handle, ensure_ascii=False, indent=2)


def _write_jsonl(path: str, values: Iterable[Json]) -> None:
    _ensure_dir(os.path.dirname(os.path.abspath(path)))
    with open(path, "w", encoding="utf-8") as handle:
        for value in values:
            handle.write(json.dumps(_jsonable(value), ensure_ascii=False) + "\n")


def _write_text(path: str, value: str) -> None:
    _ensure_dir(os.path.dirname(os.path.abspath(path)))
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(value)


def _expand_provider_value(value: Json) -> Optional[str]:
    if not isinstance(value, str):
        return None
    expanded = os.path.expandvars(value).strip()
    if not expanded or re.search(r"\$\{[^}]+\}", expanded):
        return None
    return expanded


def _first_config_value(*values: Json) -> Optional[str]:
    for value in values:
        expanded = _expand_provider_value(value)
        if expanded:
            return expanded
    return None


def _iso_from_ts(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"


def _content_text(value: Json) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: List[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "".join(parts)
    return ""


def _event_data(event: Dict[str, Json]) -> Dict[str, Json]:
    data = event.get("data")
    return data if isinstance(data, dict) else {}


def _assistant_text(event: Dict[str, Json]) -> str:
    data = _event_data(event)
    message = data.get("message") if isinstance(data.get("message"), dict) else data
    return _content_text(message.get("content") if isinstance(message, dict) else None)


def _tool_name(data: Dict[str, Json], event_type: str) -> str:
    for key in ("toolName", "tool_name", "name", "tool"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return event_type


def _tool_value(data: Dict[str, Json], keys: tuple[str, ...]) -> Json:
    for key in keys:
        if key in data:
            return data[key]
    return None


def _json_or_raw(value: Json) -> Json:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _tool_call_id(data: Dict[str, Json]) -> Optional[str]:
    for key in ("callId", "callID", "id"):
        value = data.get(key)
        if isinstance(value, (str, int)):
            return str(value)
    message = data.get("message")
    source = message.get("source") if isinstance(message, dict) else None
    value = source.get("callId") if isinstance(source, dict) else None
    return str(value) if isinstance(value, (str, int)) else None


def _tool_result_failed(data: Dict[str, Json]) -> bool:
    if data.get("error") is not None:
        return True
    message = data.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return isinstance(content, list) and any(
        isinstance(item, dict) and item.get("isError") is True for item in content
    )


def _normalize_execution_trace(
    events: List[Dict[str, Json]], *, prompt: str, started_at: float
) -> List[Dict[str, Json]]:
    trace: List[Dict[str, Json]] = [
        {
            "type": "text",
            "role": "user",
            "content": prompt,
            "startedAt": _iso_from_ts(started_at),
        }
    ]
    tool_calls: Dict[str, Dict[str, Json]] = {}
    for event in events:
        event_type = str(event.get("type") or "")
        if event_type == "assistant/message":
            text = _assistant_text(event)
            if text:
                trace.append(
                    {
                        "type": "text",
                        "role": "assistant",
                        "content": text,
                        "rawType": event_type,
                    }
                )
            continue

        data = _event_data(event)
        if event_type == "tool/call":
            call_id = _tool_call_id(data)
            name = _tool_name(data, event_type)
            trace_item: Dict[str, Json] = {
                "type": "tool",
                "role": "tool",
                "tool": name,
                "name": name,
                "callID": call_id,
                "input": _json_or_raw(
                    _tool_value(data, ("arguments", "input", "args", "command"))
                ),
                "output": None,
                "status": "started",
                "rawTypes": [event_type],
            }
            trace.append(trace_item)
            if call_id is not None:
                tool_calls[call_id] = trace_item
            continue
        if event_type == "tool/result":
            call_id = _tool_call_id(data)
            trace_item = tool_calls.get(call_id) if call_id is not None else None
            if trace_item is None:
                name = _tool_name(data, event_type)
                trace_item = {
                    "type": "tool",
                    "role": "tool",
                    "tool": name,
                    "name": name,
                    "callID": call_id,
                    "input": None,
                    "output": None,
                    "status": "started",
                    "rawTypes": [],
                }
                trace.append(trace_item)
            trace_item["output"] = _tool_value(
                data, ("message", "output", "result", "content", "response", "error")
            )
            trace_item["status"] = "failed" if _tool_result_failed(data) else "completed"
            raw_types = trace_item.get("rawTypes")
            if isinstance(raw_types, list):
                raw_types.append(event_type)
            continue

        lowered = event_type.lower()
        if "tool" not in lowered and "bash" not in lowered and "editor" not in lowered:
            continue
        status = _tool_value(data, ("status", "state"))
        name = _tool_name(data, event_type)
        trace.append(
            {
                "type": "tool",
                "role": "tool",
                "tool": name,
                "name": name,
                "callID": _tool_call_id(data),
                "input": _tool_value(
                    data,
                    ("input", "arguments", "args", "command", "request"),
                ),
                "output": _tool_value(
                    data,
                    ("output", "result", "content", "response", "error"),
                ),
                "status": str(status) if status is not None else None,
                "rawType": event_type,
            }
        )
    return trace


def _int_value(value: Json) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value >= 0 and value.is_integer():
        return int(value)
    return None


def _usage_candidates(value: Json) -> List[Dict[str, int]]:
    candidates: List[Dict[str, int]] = []
    if isinstance(value, list):
        for item in value:
            candidates.extend(_usage_candidates(item))
        return candidates
    if not isinstance(value, dict):
        return candidates

    prompt = next(
        (
            parsed
            for key in ("prompt_tokens", "promptTokens", "input_tokens", "inputTokens")
            if (parsed := _int_value(value.get(key))) is not None
        ),
        None,
    )
    completion = next(
        (
            parsed
            for key in (
                "completion_tokens",
                "completionTokens",
                "output_tokens",
                "outputTokens",
            )
            if (parsed := _int_value(value.get(key))) is not None
        ),
        None,
    )
    total = next(
        (
            parsed
            for key in ("total_tokens", "totalTokens")
            if (parsed := _int_value(value.get(key))) is not None
        ),
        None,
    )
    if prompt is not None or completion is not None or total is not None:
        prompt_value = prompt or 0
        completion_value = completion or 0
        candidates.append(
            {
                "prompt_tokens": prompt_value,
                "completion_tokens": completion_value,
                "total_tokens": total if total is not None else prompt_value + completion_value,
            }
        )
    for item in value.values():
        if isinstance(item, (dict, list)):
            candidates.extend(_usage_candidates(item))
    return candidates


def _largest_usage(value: Json) -> Optional[Dict[str, int]]:
    candidates = _usage_candidates(value)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            item["total_tokens"],
            item["prompt_tokens"],
            item["completion_tokens"],
        ),
    )


def _usage_total(events: List[Dict[str, Json]]) -> Dict[str, int]:
    # Every committed assistant message represents one model request. An event
    # can repeat the same usage object in nested protocol fields, so take the
    # largest candidate within each message and then sum across messages.
    per_request = [
        usage
        for event in events
        if event.get("type") == "assistant/message"
        if (usage := _largest_usage(event)) is not None
    ]
    if per_request:
        return {
            key: sum(item[key] for item in per_request)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        }
    fallback = _largest_usage(events)
    return fallback or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _sdk_version() -> str:
    return importlib_metadata.version(DEEPSEEK_HARNESS_DISTRIBUTION)


def _cordis_sha256() -> str:
    digest = hashlib.sha256()
    with open(DEEPSEEK_HARNESS_CORDIS_PATH, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _redact(value: str, sensitive_values: Iterable[Optional[str]]) -> str:
    redacted = str(value)
    for sensitive in sensitive_values:
        if sensitive:
            redacted = redacted.replace(sensitive, "<redacted>")
    return redacted


def _is_timeout_error(error: BaseException) -> bool:
    if isinstance(error, TimeoutError):
        return True
    text = f"{type(error).__name__}: {error}".lower()
    return "timeout" in text or "timed out" in text


def _error_result(
    message: str,
    *,
    raw_dir: str,
    started_at: float,
    status: str = "error",
) -> Dict[str, Json]:
    _write_text(os.path.join(raw_dir, "stderr.txt"), message)
    _write_text(os.path.join(raw_dir, "stdout.txt"), "")
    return {
        "status": status,
        "paths": [],
        "errorMessage": message,
        "trace": {
            "runner": "deepseekharness",
            "rawDir": raw_dir,
            "lastText": "",
            "executionTrace": [],
        },
        "metrics": {
            "turns": None,
            "promptTokens": None,
            "completionTokens": None,
            "totalTokens": None,
        },
        "durationMs": int((time.time() - started_at) * 1000),
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

    model = _first_config_value(
        api_provider.get("model") if isinstance(api_provider, dict) else None,
        os.environ.get("DSH_MODEL"),
    )
    base_url = _first_config_value(
        api_provider.get("baseUrl") if isinstance(api_provider, dict) else None,
        api_provider.get("base_url") if isinstance(api_provider, dict) else None,
        os.environ.get("DEEPSEEK_BASE_URL"),
    )
    api_key = _first_config_value(
        api_provider.get("apiKey") if isinstance(api_provider, dict) else None,
        api_provider.get("api_key") if isinstance(api_provider, dict) else None,
        os.environ.get("DEEPSEEK_API_KEY"),
    )
    runtime = (
        api_provider.get("__deepseek_harness_runtime__")
        if isinstance(api_provider.get("__deepseek_harness_runtime__"), dict)
        else {}
    )
    expected_version = str(
        runtime.get("expected_sdk_version") or DEEPSEEK_HARNESS_SDK_VERSION
    )
    provider = str(runtime.get("provider") or DEEPSEEK_HARNESS_PROVIDER)
    profile = str(runtime.get("profile") or DEEPSEEK_HARNESS_PROFILE)
    max_tokens_raw = runtime.get("max_tokens")
    max_tokens = (
        int(max_tokens_raw)
        if isinstance(max_tokens_raw, (int, float)) and int(max_tokens_raw) > 0
        else None
    )

    if not model:
        return _error_result(
            "Missing model in api_provider for DeepSeek Harness",
            raw_dir=raw_dir,
            started_at=started_at,
        )
    if not api_key:
        return _error_result(
            "Missing DEEPSEEK_API_KEY/apiProvider.apiKey for DeepSeek Harness",
            raw_dir=raw_dir,
            started_at=started_at,
        )
    if provider != DEEPSEEK_HARNESS_PROVIDER:
        return _error_result(
            f"Unsupported DeepSeek Harness provider for pinned minimal runtime: {provider}",
            raw_dir=raw_dir,
            started_at=started_at,
        )
    if profile != DEEPSEEK_HARNESS_PROFILE:
        return _error_result(
            "DeepSeek Harness profile mismatch: "
            f"required {DEEPSEEK_HARNESS_PROFILE}, config {profile}",
            raw_dir=raw_dir,
            started_at=started_at,
        )

    try:
        cordis_sha256 = _cordis_sha256()
    except OSError as error:
        return _error_result(
            f"Unable to read pinned DeepSeek Harness Cordis config: {error}",
            raw_dir=raw_dir,
            started_at=started_at,
        )
    if cordis_sha256 != DEEPSEEK_HARNESS_CORDIS_SHA256:
        return _error_result(
            "DeepSeek Harness Cordis config checksum mismatch: "
            f"required {DEEPSEEK_HARNESS_CORDIS_SHA256}, found {cordis_sha256}",
            raw_dir=raw_dir,
            started_at=started_at,
        )

    try:
        actual_version = _sdk_version()
    except importlib_metadata.PackageNotFoundError:
        return _error_result(
            f"Missing {DEEPSEEK_HARNESS_DISTRIBUTION}=={DEEPSEEK_HARNESS_SDK_VERSION}",
            raw_dir=raw_dir,
            started_at=started_at,
        )
    if expected_version != DEEPSEEK_HARNESS_SDK_VERSION or actual_version != expected_version:
        return _error_result(
            "DeepSeek Harness SDK version mismatch: "
            f"required {DEEPSEEK_HARNESS_SDK_VERSION}, config {expected_version}, found {actual_version}",
            raw_dir=raw_dir,
            started_at=started_at,
        )

    session_root = os.path.join(raw_dir, "deepseek_harness_sessions")
    _ensure_dir(session_root)
    task_id = str(agent_id or os.path.basename(os.path.abspath(sandbox_dir)) or "task")
    session_id = "workspace-bench-" + re.sub(r"[^A-Za-z0-9_.-]+", "-", task_id)
    request_timeout = timeout_s if isinstance(timeout_s, (int, float)) and timeout_s > 0 else None
    effective_config = {
        "sdkVersion": actual_version,
        "provider": provider,
        "model": model,
        "maxTokens": max_tokens,
        "profile": profile,
        "cordisPath": DEEPSEEK_HARNESS_CORDIS_PATH,
        "cordisSha256": cordis_sha256,
        "cwd": os.path.abspath(work_dir),
        "sessionRoot": os.path.abspath(session_root),
        "sessionId": session_id,
        "requestTimeoutSeconds": request_timeout,
        "baseUrlSha256": (
            hashlib.sha256(base_url.encode("utf-8")).hexdigest() if base_url else None
        ),
    }
    _write_json(os.path.join(raw_dir, "deepseek_harness_config.json"), effective_config)

    events: List[Dict[str, Json]] = []
    notifications: List[Json] = []
    final_text = ""
    finish_reason: Optional[str] = None
    error_message: Optional[str] = None
    status = "ok"
    try:
        from deepseek_harness import DeepSeekHarness

        with DeepSeekHarness(
            provider=provider,
            model=model,
            max_tokens=max_tokens,
            cwd=os.path.abspath(work_dir),
            session_root=os.path.abspath(session_root),
            cordis=DEEPSEEK_HARNESS_CORDIS_PATH,
            request_timeout_seconds=request_timeout,
            base_url=base_url,
            api_key=api_key,
        ) as harness:
            result = harness.run(str(prompt or ""), session_id=session_id)
        final_text = str(result.final_response or "")
        finish_reason = (
            str(result.finish_reason) if result.finish_reason is not None else None
        )
        events = [
            item for item in _jsonable(result.events) if isinstance(item, dict)
        ]
        notifications = list(_jsonable(result.notifications))
        if finish_reason in {None, "error", "cancelled", "canceled", "aborted", "max-tokens"}:
            status = "error"
            error_message = f"DeepSeek Harness finished with reason: {finish_reason or 'missing'}"
    except Exception as error:
        status = "timeout" if _is_timeout_error(error) else "error"
        error_message = _redact(
            f"{type(error).__name__}: {error}",
            (api_key, base_url),
        )[:4000]

    _write_jsonl(os.path.join(raw_dir, "deepseek_harness_events.jsonl"), events)
    _write_jsonl(
        os.path.join(raw_dir, "deepseek_harness_notifications.jsonl"), notifications
    )
    _write_json(
        os.path.join(raw_dir, "deepseek_harness_result.json"),
        {
            "sessionId": session_id,
            "finishReason": finish_reason,
            "finalResponse": final_text,
            "status": status,
            "errorMessage": error_message,
            "eventCount": len(events),
            "notificationCount": len(notifications),
        },
    )
    _write_text(os.path.join(raw_dir, "stdout.txt"), final_text)
    _write_text(os.path.join(raw_dir, "stderr.txt"), error_message or "")

    usage_total = _usage_total(events)
    execution_trace = _normalize_execution_trace(
        events,
        prompt=str(prompt or ""),
        started_at=started_at,
    )
    turns = sum(
        1
        for item in execution_trace
        if item.get("type") == "text" and item.get("role") == "assistant"
    )
    return {
        "status": status,
        "paths": [],
        "errorMessage": error_message,
        "trace": {
            "runner": "deepseekharness",
            "agentId": agent_id,
            "rawDir": raw_dir,
            "lastText": final_text,
            "executionTrace": execution_trace,
            "llm": {
                "provider": provider,
                "baseUrl": base_url,
                "model": model,
            },
            "usageTotal": usage_total,
            "finishReason": finish_reason,
            "sdkVersion": actual_version,
            "eventsPath": os.path.join(raw_dir, "deepseek_harness_events.jsonl"),
            "sessionRoot": session_root,
        },
        "metrics": {
            "turns": turns,
            "promptTokens": usage_total["prompt_tokens"],
            "completionTokens": usage_total["completion_tokens"],
            "totalTokens": usage_total["total_tokens"],
        },
        "durationMs": int((time.time() - started_at) * 1000),
    }
