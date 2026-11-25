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


class JudgeTraceSnapshotTest(unittest.TestCase):
    def test_preserves_output_and_folds_matching_lifecycle_records(self):
        def tool(call_id, status, output, timestamp, **extra):
            return {
                "type": "tool", "tool": "command_execution", "input": {"command": "same command"},
                "callID": call_id, "status": status, "output": output, "timestamp": timestamp, **extra,
            }

        original = [
            tool("a", "in_progress", "", 1),
            {"type": "text", "role": "assistant", "content": "progress", "timestamp": 2},
            tool("a", "completed", "stdout", 3, exitCode=0),
            tool("b", "completed", {"structured": True}, 4, exitCode=0),
            tool("c", "in_progress", "", 5),
            tool("d", "in_progress", "", 6),
            tool("d", "failed", ["failure evidence"], 7, exitCode=2),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent.json"
            path.write_text(json.dumps({"trace": {"executionTrace": original}}), encoding="utf-8")
            events = load_judge()._build_trace_snapshot(directory)["events"]

        self.assertEqual([event["timestamp"] for event in events], [2, 3, 4, 5, 7])
        self.assertEqual(events[1]["callID"], "a")
        self.assertEqual(events[1]["output"], "stdout")
        self.assertEqual(events[1]["status"], "completed")
        self.assertEqual(events[1]["startedAt"], 1)
        self.assertEqual(events[1]["exitCode"], 0)
        self.assertEqual(events[2]["output"], {"structured": True})
        self.assertEqual(events[3]["status"], "in_progress")
        self.assertEqual(events[4]["output"], ["failure evidence"])
        self.assertEqual(events[4]["exitCode"], 2)


if __name__ == "__main__":
    unittest.main()
