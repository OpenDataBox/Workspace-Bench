import argparse
import importlib.util
import json
import os
import sys
import tempfile
import unittest

import yaml


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


agent_runner = _load_module("task_selection_agent_runner", os.path.join(SRC_DIR, "agent_runner.py"))
build_run_config = _load_module("task_selection_build_run_config", os.path.join(SCRIPTS_DIR, "build_run_config.py"))


class AgentRunnerTaskSelectionTests(unittest.TestCase):
    def _write_task(self, root: str, task_id: str, persona: str) -> None:
        task_dir = os.path.join(root, task_id)
        os.makedirs(task_dir, exist_ok=True)
        with open(os.path.join(task_dir, "metadata.json"), "w", encoding="utf-8") as f:
            json.dump({"id": task_id, "persona": persona, "task": f"task {task_id}"}, f)

    def _task_root(self, td: str) -> str:
        root = os.path.join(td, "tasks")
        self._write_task(root, "45", "Product Manager")
        self._write_task(root, "55", "Backend Developer")
        self._write_task(root, "386", "Product Manager")
        return root

    def test_task_ids_select_exact_tasks_in_requested_order(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._task_root(td)
            metas = agent_runner._load_metadatas(root, task_ids=["386", "45"])

        self.assertEqual([str(meta["id"]) for meta in metas], ["386", "45"])

    def test_task_ids_fail_when_an_id_is_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._task_root(td)
            with self.assertRaisesRegex(ValueError, r"task id\(s\) not found: 999"):
                agent_runner._load_metadatas(root, task_ids=["45", "999"])

    def test_task_ids_reject_duplicates(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._task_root(td)
            with self.assertRaisesRegex(ValueError, r"duplicate task id\(s\): 45"):
                agent_runner._load_metadatas(root, task_ids=["45", "45"])

    def test_persona_selects_every_exact_match_in_dataset_order(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._task_root(td)
            metas = agent_runner._load_metadatas(root, persona="Product Manager")

        self.assertEqual([str(meta["id"]) for meta in metas], ["386", "45"])

    def test_unknown_persona_reports_available_values(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._task_root(td)
            with self.assertRaisesRegex(ValueError, "available personas: Backend Developer, Product Manager"):
                agent_runner._load_metadatas(root, persona="Researcher")

    def test_handwritten_config_cannot_combine_selectors(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._task_root(td)
            with self.assertRaisesRegex(ValueError, "mutually exclusive"):
                agent_runner._load_metadatas(root, limit=1, task_ids=["45"])

    def test_task_limit_keeps_legacy_deterministic_order(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._task_root(td)
            metas = agent_runner._load_metadatas(root, limit=2)

        self.assertEqual([str(meta["id"]) for meta in metas], ["386", "45"])


class BuildRunConfigTaskSelectionTests(unittest.TestCase):
    def _args(self, eval_root: str, **overrides):
        values = {
            "eval_root": eval_root,
            "harness": "Codex",
            "model": "kimi-k2.5",
            "model_id": None,
            "model_name": None,
            "env_prefix": None,
            "dataset": "lite",
            "run_name": None,
            "task_limit": None,
            "task_ids": None,
            "persona": None,
            "no_task_parallel": False,
            "task_parallel_workers": 1,
            "timeout_sec": 10.0,
            "eval_yaml": "runs/judge.yaml",
            "provider_type": "openai",
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_task_ids_accept_spaces_and_commas(self):
        with tempfile.TemporaryDirectory() as td:
            eval_root = os.path.join(td, "evaluation")
            path = build_run_config.build_config(
                self._args(eval_root, task_ids=["45,55", "386"])
            )
            config = yaml.safe_load(path.read_text(encoding="utf-8"))

        self.assertEqual(config["task_ids"], ["45", "55", "386"])
        self.assertEqual(config["run_name"], "Lite-Tasks-45-55-386")
        self.assertIn("tasks-45-55-386", path.name)
        self.assertNotIn("task_limit", config)

    def test_persona_is_preserved_and_names_the_run(self):
        with tempfile.TemporaryDirectory() as td:
            eval_root = os.path.join(td, "evaluation")
            path = build_run_config.build_config(
                self._args(eval_root, persona=" Product Manager ")
            )
            config = yaml.safe_load(path.read_text(encoding="utf-8"))

        self.assertEqual(config["persona"], "Product Manager")
        self.assertEqual(config["run_name"], "Lite-Persona-Product_Manager")
        self.assertIn("persona-product-manager", path.name)

    def test_programmatic_config_rejects_combined_selectors(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(SystemExit, "mutually exclusive"):
                build_run_config.build_config(
                    self._args(td, task_limit=1, persona="Product Manager")
                )

    def test_duplicate_task_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(SystemExit, r"duplicate task id\(s\): 45"):
                build_run_config.build_config(
                    self._args(td, task_ids=["45", "45"])
                )

    def test_smoke_only_applies_implicit_limit_without_a_selector(self):
        with tempfile.TemporaryDirectory() as td:
            selected_path = build_run_config.build_config(
                self._args(td, dataset="smoke", task_ids=["45"])
            )
            selected = yaml.safe_load(selected_path.read_text(encoding="utf-8"))
            default_path = build_run_config.build_config(
                self._args(td, dataset="smoke")
            )
            default = yaml.safe_load(default_path.read_text(encoding="utf-8"))

        self.assertEqual(selected["task_ids"], ["45"])
        self.assertNotIn("task_limit", selected)
        self.assertEqual(default["task_limit"], 1)


if __name__ == "__main__":
    unittest.main()
