import argparse
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest


ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
EVAL_DIR = os.path.join(ROOT_DIR, "evaluation")
SRC_DIR = os.path.join(EVAL_DIR, "src")
SCRIPTS_DIR = os.path.join(EVAL_DIR, "scripts")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def _load_module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


download_hf_assets = _load_module("download_hf_assets", os.path.join(SCRIPTS_DIR, "download_hf_assets.py"))
build_run_config = _load_module("build_run_config", os.path.join(SCRIPTS_DIR, "build_run_config.py"))
agent_as_a_judge = _load_module("agent_as_a_judge", os.path.join(SRC_DIR, "agent_as_a_judge.py"))


class DownloadLanguageMetadataTests(unittest.TestCase):
    def test_keeps_existing_english_language(self):
        meta = {
            "id": "en_case",
            "language": "en",
            "task": "Create a monthly report from the input files.",
            "rubrics": ["The report is complete."],
        }

        download_hf_assets._normalize_task_meta(meta, task_id="en_case", expected_language="en")

        self.assertEqual(meta["language"], "en")

    def test_keeps_existing_chinese_language(self):
        meta = {
            "id": "cn_case",
            "language": "cn",
            "task": "请根据输入文件生成完整的中文分析报告。",
            "rubrics": ["报告内容准确完整。"],
        }

        download_hf_assets._normalize_task_meta(meta, task_id="cn_case", expected_language="cn")

        self.assertEqual(meta["language"], "cn")

    def test_missing_language_is_detected_from_chinese_content(self):
        meta = {
            "id": "missing_case",
            "task": "请根据输入文件生成完整的中文分析报告。",
            "rubrics": ["报告内容准确完整。"],
        }

        download_hf_assets._normalize_task_meta(meta, task_id="missing_case")

        self.assertEqual(meta["language"], "cn")

    def test_metadata_content_conflict_warns_but_keeps_metadata(self):
        meta = {
            "id": "conflict_case",
            "language": "en",
            "task": "请根据输入文件生成完整的中文分析报告。",
            "rubrics": ["报告内容准确完整。"],
        }
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            download_hf_assets._normalize_task_meta(meta, task_id="conflict_case", expected_language="en")

        self.assertEqual(meta["language"], "en")
        self.assertIn("conflicts with content-detected 'cn'", stderr.getvalue())


class JudgePromptLanguageTests(unittest.TestCase):
    def test_builds_english_judge_prompt(self):
        prompt = agent_as_a_judge._build_judge_prompt(
            task_id="case",
            task_dir="/tmp/case",
            meta={
                "language": "en",
                "task": "Create a report.",
                "rubrics": ["The report exists."],
            },
            judge_view={"view_dir": "/tmp/view", "candidate_output_path": "/tmp/view/candidate_output"},
            language="en",
        )

        self.assertEqual(agent_as_a_judge._judge_system_prompt("en"), "You are a strict task evaluator.")
        self.assertIn("Evaluate the rubrics", prompt)
        self.assertIn("You are a strict task evaluator", prompt)
        self.assertIn('\\"rubrics\\": [ {\\"index\\":0,\\"passed\\":true', prompt)
        self.assertNotIn("请基于以下输入", prompt)

    def test_builds_chinese_judge_prompt(self):
        prompt = agent_as_a_judge._build_judge_prompt(
            task_id="case",
            task_dir="/tmp/case",
            meta={
                "language": "cn",
                "task": "请生成报告。",
                "rubrics": ["报告存在。"],
            },
            judge_view={"view_dir": "/tmp/view", "candidate_output_path": "/tmp/view/candidate_output"},
            language="cn",
        )

        self.assertEqual(agent_as_a_judge._judge_system_prompt("cn"), "你是一个严格的任务评测员。")
        self.assertIn("请基于以下输入 JSON", prompt)
        self.assertIn("你是一个严格的任务评测员", prompt)
        self.assertIn('\\"rubrics\\": [ {\\"index\\":0,\\"passed\\":true', prompt)


class BuildRunConfigLanguageTests(unittest.TestCase):
    def test_generated_config_defaults_to_auto_prompt_language(self):
        with tempfile.TemporaryDirectory() as td:
            eval_root = os.path.join(td, "evaluation")
            os.makedirs(os.path.join(eval_root, "runs"), exist_ok=True)
            args = argparse.Namespace(
                eval_root=eval_root,
                harness="Codex",
                model="kimi-k2.5",
                model_id=None,
                model_name=None,
                env_prefix=None,
                dataset="smoke",
                run_name=None,
                task_limit=None,
                no_task_parallel=False,
                task_parallel_workers=1,
                timeout_sec=10.0,
                eval_yaml="runs/judge.yaml",
                provider_type="openai",
            )

            config_path = build_run_config.build_config(args)
            config = build_run_config.yaml.safe_load(config_path.read_text(encoding="utf-8"))

        self.assertEqual(config["prompt_language"], "auto")
        self.assertIsNone(config["prompt_head"])
        self.assertIsNone(config["prompt_tail"])
        self.assertEqual(set(config["prompt_tail_by_language"]), {"en", "cn"})


if __name__ == "__main__":
    unittest.main()
