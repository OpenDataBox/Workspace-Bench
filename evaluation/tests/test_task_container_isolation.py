import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


EVAL_ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


isolated_runner = _load_module("isolated_runner", EVAL_ROOT / "scripts" / "run_isolated_benchmark.py")
task_entry = _load_module("task_container_entry", EVAL_ROOT / "src" / "task_container_entry.py")


class TaskContainerIsolationTests(unittest.TestCase):
    def test_selection_flags_are_replaced_by_one_task_per_container(self):
        base, selected, persona, run_name = isolated_runner._split_benchmark_args(
            [
                "--harness",
                "codex",
                "--model",
                "test-model",
                "--dataset",
                "lite",
                "--task-ids",
                "a,b",
                "c",
                "--task-parallel-workers",
                "12",
                "--run-name",
                "PaperRun",
            ]
        )
        self.assertEqual(selected, ["a", "b", "c"])
        self.assertIsNone(persona)
        self.assertEqual(run_name, "PaperRun")
        self.assertNotIn("--task-ids", base)
        self.assertNotIn("--task-parallel-workers", base)

    def test_task_selection_keeps_explicit_order_and_supports_persona(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tasks = root / "tasks_lite"
            for task_id, persona in [("2", "A"), ("10", "B"), ("3", "A")]:
                task_dir = tasks / task_id
                task_dir.mkdir(parents=True)
                (task_dir / "metadata.json").write_text(
                    json.dumps({"id": task_id, "persona": persona}), encoding="utf-8"
                )

            self.assertEqual(
                isolated_runner._selected_task_ids(root, dataset="lite", requested=["3", "2"], persona=None),
                ["3", "2"],
            )
            self.assertEqual(
                isolated_runner._selected_task_ids(root, dataset="lite", requested=[], persona="A"),
                ["2", "3"],
            )

    def test_task_entry_counts_only_files_without_following_symlinks(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside_td:
            root = Path(td)
            (root / "a.bin").write_bytes(b"12345")
            outside = Path(outside_td) / "outside.bin"
            outside.write_bytes(b"x" * 100)
            os.symlink(outside, root / "linked.bin")
            self.assertEqual(task_entry._directory_size(root), 5)

    def test_compose_task_service_has_required_reset_and_resource_controls(self):
        compose = yaml.safe_load((EVAL_ROOT / "docker" / "docker-compose.yaml").read_text(encoding="utf-8"))
        task = compose["services"]["workspace-bench-task"]
        self.assertTrue(task["read_only"])
        self.assertTrue(task["init"])
        self.assertIn("cpus", task)
        self.assertIn("mem_limit", task)
        self.assertIn("pids_limit", task)
        self.assertIn("storage_opt", task)
        self.assertIn("/workspace/Workspace-Bench:ro", task["volumes"][0])
        self.assertIn("--rm", isolated_runner._compose_command(EVAL_ROOT / "docker" / "docker-compose.yaml", "workspace-bench-task", []))

    def test_reset_integration_script_uses_two_removed_task_containers(self):
        script = (EVAL_ROOT / "scripts" / "verify_task_container_reset.py").read_text(encoding="utf-8")
        self.assertIn('"workspace-bench-task"', script)
        self.assertGreaterEqual(script.count('"--rm"'), 1)
        self.assertIn("test ! -e /tmp/", script)
        self.assertIn("docker", script)

    def test_aggregator_preserves_task_container_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_path = root / "run.yaml"
            config_path.write_text(
                yaml.safe_dump(
                    {
                        "agent_name": "Codex",
                        "model_name": "Test",
                        "run_name": "Isolated",
                        "output_dir": str(root / "output"),
                        "api_provider": {"apiKey": "secret"},
                    }
                ),
                encoding="utf-8",
            )
            case_dir = root / "output" / "Codex--Test--Isolated" / "case-1"
            case_dir.mkdir(parents=True)
            (case_dir / "agent.json").write_text(
                json.dumps(
                    {
                        "status": "passed",
                        "durationMs": 7,
                        "trace": {"outputs": {"outputManifest": [{"outputPath": "answer.txt"}]}},
                    }
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(EVAL_ROOT / "scripts" / "aggregate_isolated_run.py"),
                    "--run-config",
                    str(config_path),
                    "--task-ids",
                    "case-1",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((case_dir.parent / "agent_runner_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["summary"], {"total": 1, "passed": 1, "failed": 0, "error": 0, "timeout": 0})
            self.assertEqual(report["isolation"]["mode"], "per-task-container")
            self.assertNotIn("apiKey", report["config"]["api_provider"])


if __name__ == "__main__":
    unittest.main()
