import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / "src"


def load_judge():
    spec = importlib.util.spec_from_file_location("isolated_agent_as_a_judge", SRC / "agent_as_a_judge.py")
    judge = importlib.util.module_from_spec(spec)
    agents = types.ModuleType("agents")
    agents.claudecode = types.ModuleType("claudecode")
    inserted = str(SRC) not in sys.path
    if inserted:
        sys.path.insert(0, str(SRC))
    try:
        with patch.dict(sys.modules, {"agent_eval": types.ModuleType("agent_eval"), "agents": agents}):
            spec.loader.exec_module(judge)
    finally:
        if inserted:
            sys.path.remove(str(SRC))
    return judge


class JudgeResumeTest(unittest.TestCase):
    def test_only_complete_and_successful_results_can_be_reused(self):
        judge = load_judge()
        with tempfile.TemporaryDirectory() as directory:
            result = Path(directory) / "rubrics_judge--sample.json"
            self.assertFalse(judge._can_reuse_rubrics(str(result), 2))

            good = [{"index": 0, "passed": True}, {"index": 1, "passed": False}]
            cases = (
                ({"judge": {"error": None}, "rubrics": good}, True),
                ({"judge": {"error": "Judge output parse failed"}, "rubrics": good}, False),
                ({"judge": {"error": None}, "rubrics": good[:1]}, False),
                ({"judge": {"error": None}, "rubrics": [good[0], good[0]]}, False),
                ({"judge": {"error": None}, "rubrics": [good[0], {"index": 1, "passed": "false"}]}, False),
            )
            for content, expected in cases:
                with self.subTest(content=content):
                    result.write_text(json.dumps(content), encoding="utf-8")
                    self.assertEqual(judge._can_reuse_rubrics(str(result), 2), expected)
            result.write_text("{invalid-json", encoding="utf-8")
            self.assertFalse(judge._can_reuse_rubrics(str(result), 2))


if __name__ == "__main__":
    unittest.main()
