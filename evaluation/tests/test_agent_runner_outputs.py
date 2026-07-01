import importlib.util
import json
import os
import sys
import tempfile
import unittest


SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
MODULE_PATH = os.path.join(SRC_DIR, "agent_runner.py")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

spec = importlib.util.spec_from_file_location("agent_runner", MODULE_PATH)
agent_runner = importlib.util.module_from_spec(spec)
assert spec is not None and spec.loader is not None
spec.loader.exec_module(agent_runner)


class AgentRunnerOutputCollectionTests(unittest.TestCase):
    def _write_file(self, root: str, rel: str, content: str) -> str:
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def _run_case(
        self,
        td: str,
        run_fn,
        *,
        standard_files=None,
        meta=None,
        prompt_head="",
        prompt_tail="",
        prompt_language="auto",
        prompt_head_by_language=None,
        prompt_tail_by_language=None,
    ):
        standard = os.path.join(td, "standard")
        shared = os.path.join(td, "shared")
        runs_root = os.path.join(td, "runs")
        os.makedirs(standard, exist_ok=True)
        os.makedirs(shared, exist_ok=True)
        for rel, content in (standard_files or {}).items():
            self._write_file(standard, rel, content)

        res = agent_runner._run_one_case(
            idx=0,
            meta=meta or {"id": "case", "file_system": "role", "task": "produce output"},
            runs_root=runs_root,
            run_fn=run_fn,
            prompt_head=prompt_head,
            prompt_tail=prompt_tail,
            task_target_output_dir="model_output",
            timeout_sec=10,
            api_provider={},
            eval_while_running=False,
            eval_yaml="",
            work_dir_map={"role": shared},
            standard_work_dir_map={"role": standard},
            agent_name="TestAgent",
            model_name="TestModel",
            isolated_workdir=True,
            task_workdir_cleanup="never",
            prompt_language=prompt_language,
            prompt_head_by_language=prompt_head_by_language,
            prompt_tail_by_language=prompt_tail_by_language,
        )
        case_dir = os.path.join(runs_root, "case")
        with open(os.path.join(case_dir, "agent.json"), "r", encoding="utf-8") as f:
            agent_json = json.load(f)
        return res, agent_json, case_dir

    def test_collects_target_output_dir_and_filters_internal_files(self):
        def fake_run(*, prompt, work_dir, sandbox_dir, timeout_s, api_provider):
            out_dir = os.path.join(work_dir, "model_output")
            os.makedirs(out_dir, exist_ok=True)
            self._write_file(out_dir, "a.txt", "answer")
            self._write_file(out_dir, "trace.json", "{}")
            return {"status": "ok", "paths": [], "trace": {"lastText": ""}, "metrics": {}}

        with tempfile.TemporaryDirectory() as td:
            res, agent_json, _ = self._run_case(td, fake_run)

        self.assertEqual(res["case"]["status"], "passed")
        self.assertEqual([x["outputPath"] for x in res["case"]["outputFiles"]], ["a.txt"])
        self.assertIn("task_target_output_dir", agent_json["trace"]["outputs"]["retrievalMethod"])

    def test_mirrors_misplaced_outputs_and_filters_run_artifacts(self):
        def fake_run(*, prompt, work_dir, sandbox_dir, timeout_s, api_provider):
            self._write_file(work_dir, "report.xlsx", "workbook")
            self._write_file(work_dir, "debug.log", "noise")
            self._write_file(work_dir, ".hidden", "noise")
            return {"status": "ok", "paths": [], "trace": {"lastText": ""}, "metrics": {}}

        with tempfile.TemporaryDirectory() as td:
            res, agent_json, case_dir = self._run_case(td, fake_run)
            with open(os.path.join(case_dir, "raw", "misplaced_outputs.json"), "r", encoding="utf-8") as f:
                misplaced = json.load(f)

        output_paths = [x["outputPath"] for x in res["case"]["outputFiles"]]
        self.assertEqual(output_paths, ["_misplaced_outputs/report.xlsx"])
        self.assertEqual(misplaced[0]["sourcePath"], "report.xlsx")
        self.assertEqual(misplaced[0]["reason"], "new")
        self.assertIn("misplaced_output_diff", agent_json["trace"]["outputs"]["retrievalMethod"])

    def test_mirrors_modified_existing_files(self):
        def fake_run(*, prompt, work_dir, sandbox_dir, timeout_s, api_provider):
            self._write_file(work_dir, "docs/input.txt", "changed")
            return {"status": "ok", "paths": [], "trace": {"lastText": ""}, "metrics": {}}

        with tempfile.TemporaryDirectory() as td:
            res, _, case_dir = self._run_case(td, fake_run, standard_files={"docs/input.txt": "original"})
            with open(os.path.join(case_dir, "raw", "misplaced_outputs.json"), "r", encoding="utf-8") as f:
                misplaced = json.load(f)

        self.assertEqual([x["outputPath"] for x in res["case"]["outputFiles"]], ["_misplaced_outputs/docs/input.txt"])
        self.assertEqual(misplaced[0]["sourcePath"], "docs/input.txt")
        self.assertEqual(misplaced[0]["reason"], "modified")

    def test_timeout_with_collected_output_counts_as_passed(self):
        def fake_run(*, prompt, work_dir, sandbox_dir, timeout_s, api_provider):
            out_dir = os.path.join(work_dir, "model_output")
            os.makedirs(out_dir, exist_ok=True)
            self._write_file(out_dir, "a.txt", "partial")
            return {
                "status": "timeout",
                "paths": [],
                "trace": {"lastText": ""},
                "metrics": {},
                "errorMessage": "timed out",
            }

        with tempfile.TemporaryDirectory() as td:
            res, agent_json, _ = self._run_case(td, fake_run)

        self.assertEqual(res["case"]["status"], "passed")
        self.assertEqual(res["summary"]["passed"], 1)
        self.assertEqual(res["summary"]["timeout"], 0)
        self.assertEqual(agent_json["runnerStatus"], "timeout")
        self.assertTrue(agent_json["partialOutputCollected"])
        self.assertEqual(agent_json["errorType"], "Timeout")

    def test_wrap_prompt_uses_chinese_for_chinese_metadata(self):
        prompt = agent_runner._wrap_prompt(
            prompt="请生成报告",
            work_dir="/tmp/work",
            prompt_head="",
            prompt_tail="",
            task_target_output_dir="model_output",
            language="cn",
        )

        self.assertIn("工作目录", prompt)
        self.assertIn("输出路径列表", prompt)
        self.assertNotIn("Working Directory", prompt)

    def test_wrap_prompt_uses_english_for_english_metadata(self):
        prompt = agent_runner._wrap_prompt(
            prompt="Create the report",
            work_dir="/tmp/work",
            prompt_head="",
            prompt_tail="",
            task_target_output_dir="model_output",
            language="en",
        )

        self.assertIn("Working Directory", prompt)
        self.assertIn("Output Path List", prompt)
        self.assertNotIn("工作目录", prompt)

    def test_infers_language_from_missing_metadata(self):
        self.assertEqual(
            agent_runner._infer_language_from_meta(
                {
                    "task": "请根据输入文件生成完整的中文分析报告。",
                    "rubrics": ["报告内容准确完整。"],
                }
            ),
            "cn",
        )
        self.assertEqual(
            agent_runner._infer_language_from_meta(
                {
                    "task": "Create a concise English report from the input files.",
                    "rubrics": ["The report is accurate."],
                }
            ),
            "en",
        )

    def test_auto_prompt_language_ignores_scalar_prompt_tail_and_records_trace(self):
        seen = {}

        def fake_run(*, prompt, work_dir, sandbox_dir, timeout_s, api_provider):
            seen["prompt"] = prompt
            out_dir = os.path.join(work_dir, "model_output")
            os.makedirs(out_dir, exist_ok=True)
            self._write_file(out_dir, "a.txt", "answer")
            return {"status": "ok", "paths": [], "trace": {"lastText": ""}, "metrics": {}}

        with tempfile.TemporaryDirectory() as td:
            _, agent_json, _ = self._run_case(
                td,
                fake_run,
                meta={"id": "case", "file_system": "role", "task": "Create report", "language": "en"},
                prompt_tail="LEGACY SCALAR TAIL",
                prompt_language="auto",
            )

        self.assertNotIn("LEGACY SCALAR TAIL", seen["prompt"])
        self.assertIn("Working Directory", seen["prompt"])
        self.assertEqual(agent_json["trace"]["prompt"]["language"], "en")
        self.assertEqual(agent_json["trace"]["prompt"]["languageSource"], "metadata")
        self.assertIsNone(agent_json["trace"]["prompt"]["promptTail"])

    def test_auto_prompt_language_uses_language_specific_tail(self):
        seen = {}

        def fake_run(*, prompt, work_dir, sandbox_dir, timeout_s, api_provider):
            seen["prompt"] = prompt
            out_dir = os.path.join(work_dir, "model_output")
            os.makedirs(out_dir, exist_ok=True)
            self._write_file(out_dir, "a.txt", "answer")
            return {"status": "ok", "paths": [], "trace": {"lastText": ""}, "metrics": {}}

        with tempfile.TemporaryDirectory() as td:
            _, agent_json, _ = self._run_case(
                td,
                fake_run,
                meta={"id": "case", "file_system": "role", "task": "请根据输入文件生成完整报告", "language": "cn"},
                prompt_language="auto",
                prompt_tail_by_language={"cn": "中文尾部"},
            )

        self.assertIn("中文尾部", seen["prompt"])
        self.assertEqual(agent_json["trace"]["prompt"]["promptTail"], "中文尾部")


if __name__ == "__main__":
    unittest.main()
