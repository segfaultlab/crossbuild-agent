import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

from langchain_core.messages import AIMessage

import build_agent
import graph_agent
import knowledge

FIND_PACKAGE_FAIL = (
    "[exit=1]\n"
    "CMake Error at CMakeLists.txt:3 (find_package):\n"
    "  Could NOT find ZLIB (missing: ZLIB_LIBRARY ZLIB_INCLUDE_DIR)\n"
)
# cmake -P 跑脚本不需要编译器，拿它造一个带 find_package 报错的失败命令
FAIL_SCRIPT = 'message(FATAL_ERROR "Could NOT find ZLIB (missing: ZLIB_LIBRARY ZLIB_INCLUDE_DIR)")\n'


class AutoKnowledgeCase(unittest.TestCase):
    def setUp(self):
        self.saved_kb = (build_agent.KB_AUTO, build_agent._kb)
        build_agent.KB_AUTO = True
        build_agent._kb = knowledge.Knowledge()

    def tearDown(self):
        build_agent.KB_AUTO, build_agent._kb = self.saved_kb


class AutoKnowledge(AutoKnowledgeCase):
    def test_failed_command_gets_matching_entry(self):
        injected = []
        query, ids, note = build_agent.auto_knowledge("run_command", False, FIND_PACKAGE_FAIL, injected)
        self.assertIn("Could NOT find ZLIB", query)
        self.assertIn("k002", ids)
        self.assertTrue(note.startswith("[经验库自动匹配]"))
        self.assertIn("[k002]", note)
        self.assertEqual(injected, ids)

    def test_same_entry_is_not_attached_twice(self):
        injected = []
        _, first, _ = build_agent.auto_knowledge("run_command", False, FIND_PACKAGE_FAIL, injected)
        again = build_agent.auto_knowledge("run_command", False, FIND_PACKAGE_FAIL, injected)
        if again:
            self.assertFalse(set(first) & set(again[1]))
        self.assertEqual(len(injected), len(set(injected)))

    def test_only_failed_run_command_with_known_error(self):
        self.assertIsNone(build_agent.auto_knowledge("run_command", True, FIND_PACKAGE_FAIL, []))
        self.assertIsNone(build_agent.auto_knowledge("read_file", False, FIND_PACKAGE_FAIL, []))
        self.assertIsNone(build_agent.auto_knowledge("run_command", False, "[exit=2]\n没见过的输出\n", []))

    def test_off_by_default(self):
        build_agent.KB_AUTO = False
        self.assertIsNone(build_agent.auto_knowledge("run_command", False, FIND_PACKAGE_FAIL, []))


class RecordingClient:
    """假的 OpenAI 客户端：按脚本返回工具调用，并记下每次发给模型的最后一条消息。"""

    def __init__(self, script):
        self.script = iter(script)
        self.seen = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, messages, **kwargs):
        last = messages[-1]
        self.seen.append(last["content"] if isinstance(last, dict) else last.content)
        msg = next(self.script, SimpleNamespace(content="结束", tool_calls=None))
        return SimpleNamespace(usage=None, choices=[SimpleNamespace(message=msg)])


class RecordingModel:
    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    def invoke(self, messages):
        self.seen.append(messages[-1].content)
        return self.script.pop(0) if self.script else AIMessage(content="结束")


class RecordingTrace:
    def __init__(self, *a):
        self.run_id = 7
        self.calls = []

    def start(self, *a):
        return self.run_id

    def add_usage(self, *a):
        pass

    def tool_call(self, step, name, args, ok, result, elapsed):
        self.calls.append(name)

    def finish(self, *a):
        pass


class AgentLoops(AutoKnowledgeCase):
    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        (self.repo / "CMakeLists.txt").write_text("project(x C)\n")
        (self.repo / "fail.cmake").write_text(FAIL_SCRIPT)
        root = self.tmp / "root"
        shutil.copytree(ROOT / "toolchain", root / "toolchain")
        self.saved = (build_agent.ROOT, build_agent.WORKSPACE, build_agent.OpenAI, build_agent.Trace)
        build_agent.ROOT, build_agent.WORKSPACE = root, root / "workspace"
        os.environ.setdefault("DEEPSEEK_API_KEY", "test")

    def tearDown(self):
        build_agent.ROOT, build_agent.WORKSPACE, build_agent.OpenAI, build_agent.Trace = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def check(self, events, calls, seen):
        injects = [e for e in events if e["type"] == "kb_inject"]
        self.assertEqual(len(injects), 1)
        self.assertIn("k002", injects[0]["ids"])
        self.assertIn("auto_knowledge", calls)
        self.assertIn("[经验库自动匹配]", seen[1])
        self.assertIn("k002", events[-1]["kb_injected"])

    def test_handwritten_loop_attaches_to_tool_result(self):
        fail = SimpleNamespace(id="c0", function=SimpleNamespace(
            name="run_command", arguments=json.dumps({"command": "cmake -P fail.cmake"})))
        client = RecordingClient([SimpleNamespace(content=None, tool_calls=[fail])])
        tracer = RecordingTrace()
        build_agent.OpenAI = lambda **kw: client
        build_agent.Trace = lambda *a: tracer
        events = list(build_agent.build_events(str(self.repo), "aarch64-linux-musl", "aarch64"))
        self.check(events, tracer.calls, client.seen)

    def test_langgraph_loop_attaches_and_keeps_state(self):
        fail = AIMessage(content="", tool_calls=[{"id": "c0", "name": "run_command",
                                                  "args": {"command": "cmake -P fail.cmake"}}])
        model, tracer = RecordingModel([fail]), RecordingTrace()
        events = list(graph_agent.build_events(str(self.repo), "aarch64-linux-musl", "aarch64", model=model,
                                               tracer=tracer, checkpoint_db=self.tmp / "ckpt.db"))
        self.check(events, tracer.calls, model.seen)


if __name__ == "__main__":
    unittest.main()
