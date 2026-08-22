import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        quota_compose = yaml.safe_load(
            (EVAL_ROOT / "docker" / "docker-compose.storage-quota.yaml").read_text(encoding="utf-8")
        )
        quota_task = quota_compose["services"]["workspace-bench-task"]
        self.assertTrue(task["read_only"])
        self.assertTrue(task["init"])
        self.assertIn("cpus", task)
        self.assertIn("mem_limit", task)
        self.assertIn("pids_limit", task)
        self.assertNotIn("storage_opt", task)
        self.assertIn("storage_opt", quota_task)
        self.assertIn("/workspace/Workspace-Bench:ro", task["volumes"][0])
        self.assertNotIn(
            "/workspace/Workspace-Bench/evaluation/output",
            "\n".join(task["volumes"]),
        )
        self.assertIn("--rm", isolated_runner._compose_command(EVAL_ROOT / "docker" / "docker-compose.yaml", "workspace-bench-task", []))

    def test_task_command_uses_storage_quota_override(self):
        compose_file = EVAL_ROOT / "docker" / "docker-compose.yaml"
        quota_file = compose_file.with_name("docker-compose.storage-quota.yaml")
        command = isolated_runner._compose_command(
            compose_file,
            "workspace-bench-task",
            ["/bin/true"],
            compose_overrides=[quota_file],
        )
        self.assertEqual(
            command[:8],
            ["docker", "compose", "-f", str(compose_file), "-f", str(quota_file), "run", "--rm"],
        )

    def test_storage_quota_probe_falls_back_when_driver_rejects_it(self):
        compose_file = EVAL_ROOT / "docker" / "docker-compose.yaml"
        quota_file = compose_file.with_name("docker-compose.storage-quota.yaml")
        unsupported = subprocess.CompletedProcess(
            [],
            1,
            stdout="",
            stderr="Error response from daemon: --storage-opt is supported only for overlay over xfs with 'pquota' mount option",
        )
        calls: list[tuple[list[str], dict[str, str]]] = []

        def fake_run(command, *, cwd, env, capture=False):
            calls.append((list(command), dict(env)))
            return unsupported

        with mock.patch.object(isolated_runner, "_run", side_effect=fake_run):
            storage_quota_enforced = isolated_runner._storage_quota_available(
                compose_file=compose_file,
                cwd=EVAL_ROOT,
                env={"EXAMPLE": "1"},
            )

        self.assertFalse(storage_quota_enforced)
        self.assertEqual(len(calls), 1)
        self.assertIn(str(quota_file), calls[0][0])

    def test_storage_quota_probe_keeps_layer_quota_when_supported(self):
        compose_file = EVAL_ROOT / "docker" / "docker-compose.yaml"
        supported = subprocess.CompletedProcess([], 0, stdout="", stderr="")

        with mock.patch.object(isolated_runner, "_run", return_value=supported) as run:
            storage_quota_enforced = isolated_runner._storage_quota_available(
                compose_file=compose_file,
                cwd=EVAL_ROOT,
                env={"EXAMPLE": "1"},
            )

        self.assertTrue(storage_quota_enforced)
        self.assertIn(
            str(compose_file.with_name("docker-compose.storage-quota.yaml")),
            run.call_args.args[0],
        )

    def test_task_entry_records_storage_quota_mode(self):
        with mock.patch.dict(os.environ, {"WORKSPACE_BENCH_TASK_STORAGE_QUOTA_MODE": "case-directory-watchdog"}):
            self.assertEqual(task_entry._storage_quota_mode(), "case-directory-watchdog")
        with mock.patch.dict(os.environ, {"WORKSPACE_BENCH_TASK_STORAGE_QUOTA_MODE": "unexpected"}):
            self.assertEqual(task_entry._storage_quota_mode(), "docker-layer")

    def test_agent_task_view_redacts_rubrics_and_restores_full_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            eval_root = Path(td)
            source = eval_root / "tasks_lite" / "case-1"
            source.mkdir(parents=True)
            metadata = {
                "id": "case-1",
                "task": "Create a report",
                "data_manifest": [
                    {
                        "stored_relpath": "data/attachment.txt",
                        "target_path": "attachment.txt",
                    }
                ],
                "output_files": ["report.md"],
                "rubrics": ["SECRET CRITERION"],
                "rubric_types": ["content"],
                "rubric_notes": "SECRET NOTES",
                "judge_metadata": {"secret": True},
                "ground_truth": "SECRET ANSWER",
            }
            (source / "metadata.json").write_text(
                json.dumps(metadata),
                encoding="utf-8",
            )
            (source / "data").mkdir()
            (source / "data" / "attachment.txt").write_text("input", encoding="utf-8")
            (source / "metadata.md").write_text("SECRET RUBRIC NOTES", encoding="utf-8")
            (source / "output").mkdir()
            (source / "output" / "reference.txt").write_text("SECRET ANSWER", encoding="utf-8")

            view_root, full_metadata = isolated_runner._prepare_agent_task_view(
                eval_root,
                dataset="lite",
                task_id="case-1",
                view_token="test-view",
            )
            visible_metadata = json.loads(
                (view_root / "case-1" / "metadata.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("rubrics", visible_metadata)
            self.assertNotIn("rubric_types", visible_metadata)
            self.assertNotIn("rubric_notes", visible_metadata)
            self.assertNotIn("judge_metadata", visible_metadata)
            self.assertNotIn("ground_truth", visible_metadata)
            self.assertEqual(
                (view_root / "case-1" / "data" / "attachment.txt").read_text(encoding="utf-8"),
                "input",
            )
            self.assertFalse((view_root / "case-1" / "metadata.md").exists())
            self.assertFalse((view_root / "case-1" / "output").exists())

            runs_root = eval_root / "output" / "Codex--Test--Run"
            (runs_root / "case-1").mkdir(parents=True)
            isolated_runner._restore_evaluation_metadata(
                runs_root=runs_root,
                task_id="case-1",
                metadata=full_metadata,
            )
            restored = json.loads(
                (runs_root / "case-1" / "metadata.json").read_text(encoding="utf-8")
            )

        self.assertEqual(restored["rubrics"], ["SECRET CRITERION"])
        self.assertEqual(restored["rubric_types"], ["content"])

    def test_task_container_command_masks_private_dataset_git_and_other_runs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            view = root / "view"
            hidden = root / "hidden"
            run = root / "output" / "Codex--Test--Run"
            for path in (view, hidden, run):
                path.mkdir(parents=True)
            command = isolated_runner._compose_command(
                EVAL_ROOT / "docker" / "docker-compose.yaml",
                "workspace-bench-task",
                ["/bin/true"],
                volumes=[
                    (view, isolated_runner.CONTAINER_EVAL_ROOT / "tasks_lite", "ro"),
                    (hidden, isolated_runner.CONTAINER_EVAL_ROOT / "tasks", "ro"),
                    (hidden, isolated_runner.CONTAINER_REPO_ROOT / ".git", "ro"),
                    (hidden, isolated_runner.CONTAINER_EVAL_ROOT / "output", "ro"),
                    (
                        run,
                        isolated_runner.CONTAINER_EVAL_ROOT / "output" / run.name,
                        "rw",
                    ),
                ],
            )
            command_text = "\n".join(command)

        self.assertIn("/evaluation/tasks_lite:ro", command_text)
        self.assertIn("/evaluation/tasks:ro", command_text)
        self.assertIn("/Workspace-Bench/.git:ro", command_text)
        self.assertIn("/evaluation/output:ro", command_text)
        self.assertIn(f"/evaluation/output/{run.name}:rw", command_text)

    def test_output_mask_precreates_nested_mountpoint_and_writable_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            hidden_root = root / "hidden"
            runs_root = root / "output" / "Codex--Test--Run"

            empty_root, hidden_output_root = isolated_runner._prepare_container_output_mounts(
                hidden_root=hidden_root,
                runs_root=runs_root,
            )

            self.assertTrue(empty_root.is_dir())
            self.assertEqual(list(empty_root.iterdir()), [])
            self.assertTrue((hidden_output_root / runs_root.name).is_dir())
            self.assertTrue(runs_root.is_dir())
            self.assertEqual(runs_root.stat().st_mode & 0o777, 0o777)

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
            (case_dir / "raw").mkdir()
            (case_dir / "raw" / "container-isolation.json").write_text(
                json.dumps({"storageQuotaMode": "case-directory-watchdog"}),
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
            self.assertEqual(report["isolation"]["storageQuotaModes"], ["case-directory-watchdog"])
            self.assertNotIn("apiKey", report["config"]["api_provider"])


if __name__ == "__main__":
    unittest.main()
