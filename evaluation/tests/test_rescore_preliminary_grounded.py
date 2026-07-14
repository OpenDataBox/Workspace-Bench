import csv
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
MODULE_PATH = os.path.join(SCRIPTS_DIR, "rescore_preliminary_grounded.py")
spec = importlib.util.spec_from_file_location("rescore_preliminary_grounded", MODULE_PATH)
rescore_preliminary_grounded = importlib.util.module_from_spec(spec)
assert spec is not None and spec.loader is not None
spec.loader.exec_module(rescore_preliminary_grounded)


class PreliminaryGroundedRescoreTests(unittest.TestCase):
    def _write_audit(self, path, rows):
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["task_id", "rubric_index", "basis_label"])
            writer.writeheader()
            writer.writerows(rows)

    def _write_judge(self, root, task_id, rows):
        task_dir = os.path.join(root, task_id)
        os.makedirs(task_dir, exist_ok=True)
        path = os.path.join(task_dir, "rubrics_judge--test.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"taskId": task_id, "rubrics": rows}, handle)
        return path

    def test_rescores_selected_rubrics_without_rerunning_judge(self):
        with tempfile.TemporaryDirectory() as td:
            audit_path = os.path.join(td, "audit.csv")
            results_root = os.path.join(td, "results")
            self._write_audit(
                audit_path,
                [
                    {"task_id": "1", "rubric_index": 0, "basis_label": "有依据"},
                    {"task_id": "1", "rubric_index": 1, "basis_label": "无依据"},
                    {"task_id": "1", "rubric_index": 2, "basis_label": "有依据"},
                    {"task_id": "2", "rubric_index": 0, "basis_label": "有依据"},
                ],
            )
            self._write_judge(
                results_root,
                "1",
                [
                    {"index": 0, "passed": True},
                    {"index": 1, "passed": False},
                    {"index": 2, "passed": False},
                ],
            )
            self._write_judge(results_root, "2", [{"index": 0, "passed": True}])

            summary = rescore_preliminary_grounded.rescore(
                Path(results_root),
                Path(audit_path),
                "有依据",
            )

        self.assertEqual(summary["selection"]["selectedRubrics"], 3)
        self.assertEqual(summary["metrics"]["originalObserved"], {
            "passed": 2,
            "judged": 4,
            "microPassRate": 0.5,
            "resultUnitMacroPassRate": 2 / 3,
        })
        self.assertEqual(summary["metrics"]["preliminaryGrounded"], {
            "passed": 2,
            "judged": 3,
            "eligible": 3,
            "missing": 0,
            "microPassRate": 2 / 3,
            "resultUnitMacroPassRate": 0.75,
            "coverage": 1.0,
        })

    def test_reports_missing_selected_rubrics(self):
        with tempfile.TemporaryDirectory() as td:
            audit_path = os.path.join(td, "audit.csv")
            results_root = os.path.join(td, "results")
            self._write_audit(
                audit_path,
                [
                    {"task_id": "1", "rubric_index": 0, "basis_label": "有依据"},
                    {"task_id": "1", "rubric_index": 1, "basis_label": "有依据"},
                ],
            )
            self._write_judge(results_root, "1", [{"index": 0, "passed": True}])

            summary = rescore_preliminary_grounded.rescore(
                Path(results_root),
                Path(audit_path),
                "有依据",
            )

        grounded = summary["metrics"]["preliminaryGrounded"]
        self.assertEqual(grounded["missing"], 1)
        self.assertEqual(grounded["coverage"], 0.5)
        self.assertTrue(any("missing from judge output" in warning for warning in summary["warnings"]))


if __name__ == "__main__":
    unittest.main()
