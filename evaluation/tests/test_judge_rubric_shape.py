import importlib.util
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
SPEC = importlib.util.spec_from_file_location("judge_rubrics_fixture", SRC / "judge_rubrics.py")
VALIDATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATION)


class JudgeRubricShapeTest(unittest.TestCase):
    def test_complete_out_of_order_verdicts(self):
        rows = [{"index": 1, "passed": False}, {"index": 0, "passed": True}]
        self.assertEqual(VALIDATION.validated_rubric_rows({"rubrics": rows}, 2), rows)

    def test_invalid_or_incomplete_verdicts(self):
        for rows in (
            [{"index": 0, "passed": True}],
            [{"index": 0, "passed": True}, {"index": 0, "passed": True}],
            [{"index": -1, "passed": True}, {"index": 0, "passed": True}],
            [{"index": 2, "passed": True}, {"index": 0, "passed": True}],
            [{"index": True, "passed": True}, {"index": 0, "passed": True}],
            [{"index": 1, "passed": "true"}, {"index": 0, "passed": True}],
        ):
            with self.subTest(rows=rows):
                self.assertIsNone(VALIDATION.validated_rubric_rows({"rubrics": rows}, 2))


if __name__ == "__main__":
    unittest.main()
