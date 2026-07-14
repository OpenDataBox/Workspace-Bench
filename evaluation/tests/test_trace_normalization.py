import importlib.util
import json
import os
import sys
import tempfile
import unittest


SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def _load_module(name: str, filename: str):
    path = os.path.join(SRC_DIR, "agents", filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


codex = _load_module("trace_codex", "codex.py")
deepagent = _load_module("trace_deepagent", "deepagent.py")
openclaw = _load_module("trace_openclaw", "openclaw.py")


class TraceNormalizationTests(unittest.TestCase):
    def test_codex_keeps_string_command_output(self):
        parsed = codex.parse_codex_jsonl(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-1",
                        "type": "command_execution",
                        "command": "echo created report",
                        "aggregated_output": "created report\n",
                        "exit_code": 0,
                        "status": "completed",
                    },
                }
            ),
            prompt="create a report",
            started_at=1_700_000_000.0,
            base_url="https://example.test/v1",
            model="example-model",
        )

        tool_event = next(event for event in parsed["executionTrace"] if event["type"] == "tool")
        self.assertEqual(tool_event["output"], "created report\n")
        self.assertEqual(tool_event["exitCode"], 0)

    def test_deepagent_keeps_tool_content_and_artifact(self):
        trace = deepagent._build_execution_trace(
            prompt="create a report",
            started_at=1_700_000_000.0,
            messages=[
                {"type": "human", "content": "create a report"},
                {
                    "type": "ai",
                    "content": "",
                    "tool_calls": [
                        {"id": "call-1", "name": "write_file", "args": {"path": "report.doc"}}
                    ],
                    "usage_metadata": {},
                },
                {
                    "type": "tool",
                    "tool_call_id": "call-1",
                    "name": "write_file",
                    "content": "created report",
                    "artifact": {"path": "report.doc", "size": 42},
                },
            ],
            base_url="https://example.test/v1",
            model="example-model",
            provider="openai",
        )

        tool_event = next(event for event in trace["executionTrace"] if event["type"] == "tool")
        self.assertEqual(tool_event["output"]["content"], "created report")
        self.assertEqual(tool_event["output"]["artifact"]["path"], "report.doc")

    def test_openclaw_preserves_structured_tool_result_and_error_flag(self):
        content = [
            {"type": "text", "text": "write failed"},
            {"type": "image", "media_type": "image/png", "data": "..."},
        ]
        session = [
            {
                "type": "message",
                "timestamp": "2026-07-15T10:00:00.000Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "toolCall", "id": "call-1", "name": "exec", "arguments": {"command": "false"}}
                    ],
                },
            },
            {
                "type": "message",
                "timestamp": "2026-07-15T10:00:01.000Z",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "call-1",
                    "toolName": "exec",
                    "content": content,
                    "artifact": {"exitCode": 1},
                    "isError": True,
                    "details": {"status": "failed", "exitCode": 1},
                },
            },
        ]

        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", encoding="utf-8", delete=False) as f:
            for event in session:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
            path = f.name
        try:
            trace = openclaw._extract_openclaw_trace(
                session_jsonl_path=path,
                base_url="https://example.test/v1",
                model="example-model",
            )
        finally:
            os.unlink(path)

        tool_event = next(event for event in trace["executionTrace"] if event["type"] == "tool")
        self.assertEqual(tool_event["output"]["content"], content)
        self.assertEqual(tool_event["output"]["artifact"]["exitCode"], 1)
        self.assertTrue(tool_event["output"]["isError"])
        self.assertEqual(tool_event["status"], "failed")


if __name__ == "__main__":
    unittest.main()
