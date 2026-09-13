import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT))

import build_agent
import tools
from tools import ToolError

HAS_TOOLCHAIN = shutil.which("zig") and shutil.which("cmake")


class TmpDir(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class PathBoundary(TmpDir):
    def test_sibling_with_same_prefix_is_outside(self):
        other = self.tmp / "repo-other"
        other.mkdir()
        (other / "secret.txt").write_text("secret")
        with self.assertRaises(ToolError):
            tools.read_file(self.repo, "../repo-other/secret.txt")

    def test_relative_command_argument_cannot_escape(self):
        (self.tmp / "outside.txt").write_text("outside")
        for cmd in ("cmake -E cat ../outside.txt", "ls ..", "cmake -DX=../outside.txt -E echo",
                    "ls ../repo-other"):
            with self.subTest(cmd=cmd), self.assertRaises(ToolError):
                tools.run_command(self.repo, cmd)

    def test_inside_arguments_still_work(self):
        (self.repo / "inside.txt").write_text("hello")
        out = tools.run_command(self.repo, "cmake -E cat inside.txt")
        self.assertTrue(out.startswith("[exit=0]"))
        self.assertIn("hello", out)


class Output(TmpDir):
    def test_exit_code_survives_truncation_and_log_is_readable(self):
        (self.repo / "big.txt").write_text("\n".join(f"line {i}" for i in range(500)))
        out = tools.run_command(self.repo, "cmake -E cat big.txt missing.txt")
        self.assertRegex(out, r"^\[exit=[1-9]\d*\]")
        log = out.split("完整输出: ", 1)[1].split("，", 1)[0]
        self.assertTrue(log.startswith(".xbuild/logs/"))
        self.assertIn("line 0", tools.read_file(self.repo, log))

    def test_read_file_pages_from_the_head(self):
        (self.repo / "CMakeLists.txt").write_text("\n".join(f"L{i}" for i in range(1, 251)))
        first = tools.read_file(self.repo, "CMakeLists.txt")
        self.assertIn("L1\n", first)
        self.assertIn("offset=101", first)
        rest = tools.read_file(self.repo, "CMakeLists.txt", offset=201)
        self.assertIn("L201", rest)
        self.assertTrue(rest.endswith("L250"))

    def test_small_file_is_returned_as_is(self):
        (self.repo / "a.txt").write_text("x\ny")
        self.assertEqual(tools.read_file(self.repo, "a.txt"), "x\ny")


class Prepare(TmpDir):
    def test_dot_names_are_rejected_without_deleting(self):
        workspace = self.tmp / "workspace"
        marker = self.tmp / "keep.txt"
        marker.write_text("keep")
        for source in ("..", ".", "/", "foo/.."):
            with self.subTest(source=source), self.assertRaises(ValueError):
                build_agent.prepare(source, workspace)
        self.assertTrue(marker.exists())
        self.assertTrue(workspace.exists())


@unittest.skipUnless(HAS_TOOLCHAIN, "需要 zig 和 cmake")
class Verify(TmpDir):
    def setUp(self):
        super().setUp()
        os.environ["ZIG_TARGET"] = "aarch64-linux-musl"
        os.environ["ZIG_ARCH"] = "aarch64"

    def configure(self, cmakelists, toolchain=True):
        (self.repo / "CMakeLists.txt").write_text(cmakelists)
        (self.repo / "foo.c").write_text("int foo(void) { return 1; }\n")
        project = build_agent.prepare(str(self.repo), self.tmp / "workspace")
        env = {k: v for k, v in os.environ.items() if k != "CMAKE_TOOLCHAIN_FILE"}
        if toolchain:
            env["CMAKE_TOOLCHAIN_FILE"] = str(project / ".xbuild" / "zig.cmake")
        subprocess.run(["cmake", "-B", "build"], cwd=project, env=env, check=True, capture_output=True)
        return project

    def test_project_without_targets_fails(self):
        project = self.configure("cmake_minimum_required(VERSION 3.20)\nproject(empty C)\n")
        passed, log = build_agent.verify(project, "aarch64")
        self.assertFalse(passed)
        self.assertIn("没有 aarch64", log)

    def test_cross_compiled_library_passes(self):
        project = self.configure(
            "cmake_minimum_required(VERSION 3.20)\nproject(lib C)\nadd_library(foo STATIC foo.c)\n")
        passed, log = build_agent.verify(project, "aarch64")
        self.assertTrue(passed, log)

    def test_host_build_fails(self):
        project = self.configure(
            "cmake_minimum_required(VERSION 3.20)\nproject(lib C)\nadd_library(foo STATIC foo.c)\n",
            toolchain=False)
        passed, _ = build_agent.verify(project, "aarch64")
        self.assertFalse(passed)

    def test_build_dir_other_than_build_is_found_and_dependency_dirs_ignored(self):
        (self.repo / "build").mkdir()
        (self.repo / "build" / "script.sh").write_text("echo not a cmake dir\n")
        (self.repo / "dep").mkdir()
        (self.repo / "dep" / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.20)\nproject(dep C)\n")
        (self.repo / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.20)\nproject(lib C)\nadd_library(foo STATIC foo.c)\n")
        (self.repo / "foo.c").write_text("int foo(void) { return 1; }\n")
        project = build_agent.prepare(str(self.repo), self.tmp / "workspace")
        env = {**os.environ, "CMAKE_TOOLCHAIN_FILE": str(project / ".xbuild" / "zig.cmake")}
        subprocess.run(["cmake", "-B", "out"], cwd=project, env=env, check=True, capture_output=True)
        subprocess.run(["cmake", "-S", "dep", "-B", "dep-build"], cwd=project, env=env, check=True, capture_output=True)
        self.assertEqual(build_agent.find_build_dir(project), (project / "out").resolve())
        passed, log = build_agent.verify(project, "aarch64")
        self.assertTrue(passed, log)

    def test_wrong_architecture_fails(self):
        project = self.configure(
            "cmake_minimum_required(VERSION 3.20)\nproject(lib C)\nadd_library(foo STATIC foo.c)\n")
        passed, log = build_agent.verify(project, "x86_64")
        self.assertFalse(passed)
        self.assertIn("不是 x86_64", log)


def tool_msg(i, name, args):
    call = SimpleNamespace(id=f"c{i}", function=SimpleNamespace(name=name, arguments=json.dumps(args)))
    return SimpleNamespace(content=None, tool_calls=[call])


class FakeClient:
    def __init__(self, script):
        self.script = iter(script)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        msg = next(self.script, SimpleNamespace(content="结束", tool_calls=None))
        return SimpleNamespace(usage=None, choices=[SimpleNamespace(message=msg)])


class FakeTrace:
    calls = []

    def __init__(self, *a):
        FakeTrace.calls = []

    def start(self, *a):
        return 1

    def add_usage(self, *a):
        pass

    def tool_call(self, step, name, args, ok, result, elapsed):
        FakeTrace.calls.append((name, ok, result))

    def finish(self, *a):
        pass


class AgentLoop(TmpDir):
    def test_failed_command_is_not_ok_and_edit_resets_repeat_count(self):
        (self.repo / "CMakeLists.txt").write_text("project(x C)\n")
        bad = {"command": "cmake -E cat missing.txt"}
        script = [tool_msg(i, "run_command", bad) for i in range(3)]
        script.append(tool_msg(3, "write_file", {"path": "missing.txt", "content": "now here"}))
        script.append(tool_msg(4, "run_command", bad))

        saved = (build_agent.OpenAI, build_agent.Trace, build_agent.ROOT, build_agent.WORKSPACE)
        workspace_root = self.tmp / "root"
        shutil.copytree(ROOT / "toolchain", workspace_root / "toolchain")
        build_agent.OpenAI = lambda **kw: FakeClient(script)
        build_agent.Trace = FakeTrace
        build_agent.ROOT = workspace_root
        build_agent.WORKSPACE = workspace_root / "workspace"
        os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        try:
            events = list(build_agent.build_events(str(self.repo), "aarch64-linux-musl", "aarch64"))
        finally:
            build_agent.OpenAI, build_agent.Trace, build_agent.ROOT, build_agent.WORKSPACE = saved

        runs = [c for c in FakeTrace.calls if c[0] == "run_command"]
        self.assertEqual([ok for _, ok, _ in runs], [False, False, False, True])
        self.assertIn("now here", runs[-1][2])
        self.assertFalse(events[-1]["passed"])
        self.assertEqual(os.environ["CMAKE_TOOLCHAIN_FILE"],
                         str(workspace_root / "workspace" / "repo" / ".xbuild" / "zig.cmake"))

    def test_toolchain_env_applies_to_subdirectory_configure(self):
        if not HAS_TOOLCHAIN:
            self.skipTest("需要 zig 和 cmake")
        (self.repo / "dep").mkdir()
        (self.repo / "dep" / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.21)\nproject(dep C)\nadd_library(d STATIC d.c)\n")
        (self.repo / "dep" / "d.c").write_text("int d(void) { return 0; }\n")
        project = build_agent.prepare(str(self.repo), self.tmp / "workspace")
        env = {**os.environ, "ZIG_TARGET": "aarch64-linux-musl", "ZIG_ARCH": "aarch64",
               "CMAKE_TOOLCHAIN_FILE": str(project / ".xbuild" / "zig.cmake")}
        subprocess.run(["cmake", "-S", "dep", "-B", "dep/build"], cwd=project, env=env,
                       check=True, capture_output=True)
        subprocess.run(["cmake", "--build", "dep/build"], cwd=project, env=env,
                       check=True, capture_output=True)
        hits, foreign = build_agent.scan_artifacts(project / "dep" / "build", "aarch64")
        self.assertTrue(hits)
        self.assertFalse(foreign)


class ServerLock(unittest.TestCase):
    def test_lock_is_held_until_build_thread_finishes(self):
        from server import app as server

        release = threading.Event()

        def slow_build(*a):
            yield {"type": "run_started"}
            release.wait(5)
            yield {"type": "finished"}

        saved = server.build_agent.build_events
        server.build_agent.build_events = slow_build
        try:
            resp = server.api_build(source="https://example.com/x.git")
            del resp
            time.sleep(0.2)
            self.assertTrue(server.running.locked())
            release.set()
            for _ in range(50):
                if not server.running.locked():
                    break
                time.sleep(0.05)
            self.assertFalse(server.running.locked())
        finally:
            server.build_agent.build_events = saved
            release.set()


if __name__ == "__main__":
    unittest.main()
