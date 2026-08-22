import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "agents" / "deepseekharness.py"
SPEC = importlib.util.spec_from_file_location("deepseek_harness_adapter", MODULE_PATH)
deepseekharness = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(deepseekharness)


class _FakeResult:
    final_response = "Done. Wrote model_output/result.txt"
    finish_reason = "completed"
    events = [
        {
            "type": "tool/call",
            "data": {
                "callId": "call-1",
                "name": "bash",
                "arguments": '{"command":"mkdir -p model_output"}',
            },
        },
        {
            "type": "tool/result",
            "data": {
                "message": {
                    "source": {"kind": "tool", "callId": "call-1"},
                    "content": [{"isError": False, "content": ""}],
                }
            },
        },
        {
            "type": "assistant/message",
            "data": {
                "message": {
                    "content": [
                        {"type": "text", "text": "Done. Wrote model_output/result.txt"}
                    ]
                },
                "usage": {
                    "promptTokens": 120,
                    "completionTokens": 30,
                    "totalTokens": 150,
                },
            },
        },
    ]
    notifications = []


class _FakeHarness:
    last_kwargs = None
    last_session_id = None

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        return None

    def run(self, _prompt, *, session_id):
        type(self).last_session_id = session_id
        return _FakeResult()


class DeepSeekHarnessAdapterTests(unittest.TestCase):
    def _provider(self, **overrides):
        provider = {
            "baseUrl": "https://provider.example/v1",
            "apiKey": "secret-key",
            "model": "deepseek-v4-flash",
            "__deepseek_harness_runtime__": {
                "expected_sdk_version": "0.1.0rc7",
                "provider": "deepseek-official",
                "profile": "jsonrpc-agent-office-skills-12c3f46",
                "max_tokens": 49152,
            },
        }
        provider.update(overrides)
        return provider

    def test_success_uses_official_sdk_and_preserves_raw_events(self):
        fake_module = types.ModuleType("deepseek_harness")
        fake_module.DeepSeekHarness = _FakeHarness
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            deepseekharness, "_sdk_version", return_value="0.1.0rc7"
        ), mock.patch.dict(sys.modules, {"deepseek_harness": fake_module}):
            work_dir = os.path.join(td, "work")
            sandbox_dir = os.path.join(td, "case")
            os.makedirs(work_dir)
            result = deepseekharness.run(
                prompt="Create the result",
                work_dir=work_dir,
                sandbox_dir=sandbox_dir,
                timeout_s=123,
                api_provider=self._provider(),
                agent_id="task 45",
            )

            raw_dir = Path(sandbox_dir) / "raw"
            config = json.loads(
                (raw_dir / "deepseek_harness_config.json").read_text(encoding="utf-8")
            )
            events = (raw_dir / "deepseek_harness_events.jsonl").read_text(
                encoding="utf-8"
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["trace"]["lastText"], _FakeResult.final_response)
        self.assertEqual(result["metrics"]["promptTokens"], 120)
        self.assertEqual(result["metrics"]["completionTokens"], 30)
        self.assertEqual(result["metrics"]["totalTokens"], 150)
        tool_events = [
            item
            for item in result["trace"]["executionTrace"]
            if item.get("type") == "tool"
        ]
        self.assertEqual(len(tool_events), 1)
        self.assertEqual(tool_events[0]["tool"], "bash")
        self.assertEqual(tool_events[0]["callID"], "call-1")
        self.assertEqual(tool_events[0]["input"], {"command": "mkdir -p model_output"})
        self.assertEqual(tool_events[0]["status"], "completed")
        self.assertEqual(tool_events[0]["rawTypes"], ["tool/call", "tool/result"])
        self.assertEqual(_FakeHarness.last_kwargs["provider"], "deepseek-official")
        self.assertEqual(_FakeHarness.last_kwargs["model"], "deepseek-v4-flash")
        self.assertEqual(_FakeHarness.last_kwargs["max_tokens"], 49152)
        self.assertEqual(
            _FakeHarness.last_kwargs["cordis"],
            deepseekharness._profile_spec(deepseekharness.DEEPSEEK_HARNESS_PROFILE)["cordis_path"],
        )
        self.assertEqual(
            _FakeHarness.last_kwargs["env"],
            {"WORKSPACE_BENCH_DSH_SKILLS_DIR": deepseekharness.DEEPSEEK_HARNESS_SKILLS_DIR},
        )
        self.assertEqual(_FakeHarness.last_kwargs["request_timeout_seconds"], 123)
        self.assertEqual(_FakeHarness.last_session_id, "workspace-bench-task-45")
        self.assertEqual(config["sdkVersion"], "0.1.0rc7")
        self.assertEqual(config["profile"], "jsonrpc-agent-office-skills-12c3f46")
        self.assertEqual(
            config["cordisSha256"],
            "12c3f46e55a2306197b7811844430c21ff7736a643e74acb33fded6b42e127c4",
        )
        self.assertTrue(config["skillsEnabled"])
        self.assertEqual(config["skillsDir"], deepseekharness.DEEPSEEK_HARNESS_SKILLS_DIR)
        self.assertNotIn("secret-key", json.dumps(config))
        self.assertNotIn("https://provider.example/v1", json.dumps(config))
        self.assertIn('"type": "assistant/message"', events)

    def test_pinned_cordis_profiles_and_office_skills_config(self):
        minimal = deepseekharness._profile_spec("jsonrpc-agent-minimal-99f6f02")
        office = deepseekharness._profile_spec(deepseekharness.DEEPSEEK_HARNESS_PROFILE)
        self.assertEqual(
            deepseekharness._cordis_sha256(minimal["cordis_path"]),
            minimal["cordis_sha256"],
        )
        self.assertEqual(deepseekharness._cordis_sha256(office["cordis_path"]), office["cordis_sha256"])
        office_text = Path(office["cordis_path"]).read_text(encoding="utf-8")
        self.assertIn("@deepseek-ai/dsh-agent-spine-demo", office_text)
        self.assertIn("skills:\n      enabled: true", office_text)
        self.assertIn("includeDefaultRoots: false", office_text)
        self.assertIn("customSkillDirs:", office_text)
        self.assertIn("WORKSPACE_BENCH_DSH_SKILLS_DIR", office_text)

    def test_usage_sums_model_requests_without_double_counting_nested_usage(self):
        events = [
            {
                "type": "assistant/message",
                "data": {
                    "usage": {"inputTokens": 7, "outputTokens": 9, "totalTokens": 16},
                    "nested": {
                        "usage": {"inputTokens": 7, "outputTokens": 9, "totalTokens": 16}
                    },
                },
            },
            {
                "type": "assistant/message",
                "data": {
                    "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}
                },
            },
        ]

        self.assertEqual(
            deepseekharness._usage_total(events),
            {"prompt_tokens": 18, "completion_tokens": 14, "total_tokens": 32},
        )

    def test_missing_api_key_fails_before_importing_sdk(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": ""},
        ):
            result = deepseekharness.run(
                prompt="task",
                work_dir=td,
                sandbox_dir=os.path.join(td, "case"),
                timeout_s=1,
                api_provider=self._provider(apiKey="${MISSING_KEY}"),
            )

        self.assertEqual(result["status"], "error")
        self.assertIn("Missing DEEPSEEK_API_KEY", result["errorMessage"])

    def test_version_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            deepseekharness, "_sdk_version", return_value="0.1.0rc6"
        ):
            result = deepseekharness.run(
                prompt="task",
                work_dir=td,
                sandbox_dir=os.path.join(td, "case"),
                timeout_s=1,
                api_provider=self._provider(),
            )

        self.assertEqual(result["status"], "error")
        self.assertIn("version mismatch", result["errorMessage"])
        self.assertIn("0.1.0rc6", result["errorMessage"])

    def test_unknown_profile_fails_closed(self):
        provider = self._provider()
        provider["__deepseek_harness_runtime__"]["profile"] = "some-other-profile"
        with tempfile.TemporaryDirectory() as td:
            result = deepseekharness.run(
                prompt="task",
                work_dir=td,
                sandbox_dir=os.path.join(td, "case"),
                timeout_s=1,
                api_provider=provider,
            )

        self.assertEqual(result["status"], "error")
        self.assertIn("unsupported DeepSeek Harness profile", result["errorMessage"])

    def test_sdk_timeout_maps_to_runner_timeout_and_redacts_credentials(self):
        class TimeoutHarness(_FakeHarness):
            def run(self, _prompt, *, session_id):
                raise TimeoutError(
                    "request to https://provider.example/v1 with secret-key timed out"
                )

        fake_module = types.ModuleType("deepseek_harness")
        fake_module.DeepSeekHarness = TimeoutHarness
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            deepseekharness, "_sdk_version", return_value="0.1.0rc7"
        ), mock.patch.dict(sys.modules, {"deepseek_harness": fake_module}):
            result = deepseekharness.run(
                prompt="task",
                work_dir=td,
                sandbox_dir=os.path.join(td, "case"),
                timeout_s=1,
                api_provider=self._provider(),
            )

        self.assertEqual(result["status"], "timeout")
        self.assertNotIn("secret-key", result["errorMessage"])
        self.assertNotIn("https://provider.example/v1", result["errorMessage"])
        self.assertIn("<redacted>", result["errorMessage"])


if __name__ == "__main__":
    unittest.main()
