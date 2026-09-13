import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

from langchain_core.messages import AIMessage

import build_agent
import graph_agent


def call(i, name, args):
    return AIMessage(content="", tool_calls=[{"id": f"c{i}", "name": name, "args": args}])


class FakeModel:
    def __init__(self, script, fail_at=None):
        self.script = list(script)
        self.fail_at = fail_at
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        if self.fail_at == self.calls:
            raise ConnectionError("模拟 API 中断")
        return self.script.pop(0) if self.script else AIMessage(content="结束")


class FakeTrace:
    def __init__(self):
        self.run_id = None
        self.calls = []
        self.finished = []

    def start(self, *a):
        self.run_id = 7
        return self.run_id

    def add_usage(self, usage):
        pass

    def tool_call(self, step, name, args, ok, result, elapsed):
        self.calls.append((step, name, ok, result))

    def finish(self, status, steps, result):
        self.finished.append(status)


class GraphAgent(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        (self.repo / "CMakeLists.txt").write_text("project(x C)\n")
        root = self.tmp / "root"
        shutil.copytree(ROOT / "toolchain", root / "toolchain")
        self.saved = (build_agent.ROOT, build_agent.WORKSPACE)
        build_agent.ROOT, build_agent.WORKSPACE = root, root / "workspace"
        self.db = self.tmp / "ckpt.db"

    def tearDown(self):
        build_agent.ROOT, build_agent.WORKSPACE = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def events(self, model, tracer, resume=None):
        return list(graph_agent.build_events(str(self.repo), "aarch64-linux-musl", "aarch64", resume=resume,
                                             model=model, tracer=tracer, checkpoint_db=self.db))

    def test_guardrails_match_handwritten_loop(self):
        bad = {"command": "cmake -E cat missing.txt"}
        script = [call(i, "run_command", bad) for i in range(3)]
        script.append(call(3, "write_file", {"path": "missing.txt", "content": "now here"}))
        script.append(call(4, "run_command", bad))
        tracer = FakeTrace()
        events = self.events(FakeModel(script), tracer)
        runs = [c for c in tracer.calls if c[1] == "run_command"]
        self.assertEqual([ok for _, _, ok, _ in runs], [False, False, False, True])
        self.assertIn("now here", runs[-1][3])
        self.assertEqual(events[-1]["type"], "finished")
        self.assertEqual(events[-1]["status"], "failed")
        self.assertEqual(events[-1]["steps"], 6)
        self.assertEqual(os.environ["CMAKE_TOOLCHAIN_FILE"],
                         str(build_agent.ROOT / "workspace" / "repo" / ".xbuild" / "zig.cmake"))

    def test_resume_after_crash_does_not_repeat_finished_steps(self):
        script = [call(0, "write_file", {"path": "a.txt", "content": "1"}),
                  call(1, "run_command", {"command": "cmake -E cat a.txt"})]
        tracer = FakeTrace()
        with self.assertRaises(ConnectionError):
            self.events(FakeModel(script, fail_at=3), tracer)
        self.assertEqual([c[1] for c in tracer.calls], ["write_file", "run_command"])
        self.assertEqual(tracer.finished, ["crash:ConnectionError"])

        resumed = FakeTrace()
        script2 = [call(2, "run_command", {"command": "cmake -E echo resumed"})]
        events = self.events(FakeModel(script2), resumed, resume="run-7")
        self.assertTrue(events[0]["resumed"])
        self.assertEqual([c[1] for c in resumed.calls], ["run_command"])
        self.assertEqual(resumed.calls[0][0], 3)
        self.assertEqual(events[-1]["steps"], 4)
        self.assertEqual(resumed.finished, ["failed"])

    def test_step_limit_routes_to_verify(self):
        saved = build_agent.MAX_STEPS
        build_agent.MAX_STEPS = 3
        try:
            script = [call(i, "run_command", {"command": f"cmake -E echo {i}"}) for i in range(10)]
            events = self.events(FakeModel(script), FakeTrace())
        finally:
            build_agent.MAX_STEPS = saved
        self.assertEqual(events[-1]["steps"], 3)
        self.assertEqual(events[-1]["status"], "max_steps")


if __name__ == "__main__":
    unittest.main()
