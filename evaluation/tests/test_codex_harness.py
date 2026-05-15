import importlib.util
import json
import os
import shutil
import tempfile
import unittest


MODULE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "agents", "codex.py"))
spec = importlib.util.spec_from_file_location("codex_harness", MODULE_PATH)
codex = importlib.util.module_from_spec(spec)
assert spec is not None and spec.loader is not None
spec.loader.exec_module(codex)


class CodexHarnessTests(unittest.TestCase):
    def test_parse_codex_jsonl_extracts_text_and_usage(self):
        stdout = "\n".join(
            [
                '{"type":"agent_message","message":"created file","usage":{"input_tokens":12,"output_tokens":5}}',
                '{"type":"response.output_text.delta","delta":"[\\\"model_output/a.txt\\\"]"}',
            ]
        )

        parsed = codex.parse_codex_jsonl(
            stdout,
            prompt="do work",
            started_at=1_700_000_000.0,
            base_url="https://example.test/v1",
            model="example-model",
        )

        self.assertEqual(parsed["lastText"], '["model_output/a.txt"]')
        self.assertEqual(parsed["usageTotal"]["prompt_tokens"], 12)
        self.assertEqual(parsed["usageTotal"]["completion_tokens"], 5)
        self.assertEqual(parsed["turns"], 2)
        self.assertEqual(parsed["executionTrace"][0]["role"], "user")
        self.assertEqual(parsed["executionTrace"][-1]["role"], "assistant")

    def test_parse_real_codex_item_events(self):
        stdout = "\n".join(
            [
                json.dumps({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "working"}}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "item_2",
                            "type": "command_execution",
                            "command": "pwd",
                            "aggregated_output": "/tmp\n",
                            "exit_code": 0,
                            "status": "completed",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 20, "cached_input_tokens": 5, "output_tokens": 7, "reasoning_output_tokens": 2},
                    }
                ),
            ]
        )
        parsed = codex.parse_codex_jsonl(
            stdout,
            prompt="do work",
            started_at=1_700_000_000.0,
            base_url="https://example.test/v1",
            model="example-model",
        )
        self.assertEqual(parsed["lastText"], "working")
        self.assertEqual(parsed["usageTotal"]["cache_read"], 5)
        self.assertEqual(parsed["usageTotal"]["cache_write"], 2)
        self.assertTrue(any(x.get("type") == "tool" for x in parsed["executionTrace"]))

    def test_run_reports_missing_model_before_invoking_codex(self):
        with tempfile.TemporaryDirectory() as td:
            res = codex.run(
                prompt="noop",
                work_dir=td,
                sandbox_dir=os.path.join(td, "sandbox"),
                timeout_s=1,
                api_provider={"apiKey": "dummy"},
            )

        if shutil.which("codex"):
            self.assertEqual(res["status"], "error")
            self.assertIn("Missing model", res["errorMessage"])


if __name__ == "__main__":
    unittest.main()
