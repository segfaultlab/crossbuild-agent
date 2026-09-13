import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from openai import OpenAI

import errors
from knowledge import Knowledge
from mini_agent import load_env
from tools import REGISTRY, SCHEMAS, ToolError
from trace import Trace

load_env()

ROOT = Path(__file__).resolve().parent.parent
MODEL = os.environ.get("MODEL", "deepseek-flash")
MAX_STEPS = int(os.environ.get("MAX_STEPS", "40"))
REPEAT_LIMIT = 3
ELF_MACHINES = {"aarch64": 183, "x86_64": 62, "arm": 40, "riscv64": 243, "x86": 3, "i386": 3}
USE_KB = os.environ.get("USE_KB", "0") == "1"

KB_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_knowledge",
        "description": "交叉编译经验库。**每次 configure 或 build 失败后都先查一次**，"
                       "把报错的关键行作为查询，再决定怎么改。它会给出这类报错的成因和处理办法，"
                       "能省掉大量试错。没有报错时不要调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "报错信息原文或关键症状描述"},
            },
            "required": ["query"],
        },
    },
}

_kb = Knowledge() if USE_KB else None


def search_knowledge(workdir, query):
    return _kb.format(_kb.search(query, topk=3))


def tool_set():
    if not USE_KB:
        return SCHEMAS, REGISTRY
    return SCHEMAS + [KB_SCHEMA], {**REGISTRY, "search_knowledge": search_knowledge}

SYSTEM_PROMPT = """你的任务是把一个 C/C++ 项目交叉编译到目标平台。

环境：
- 工作目录就是项目根目录，你只能在里面操作
- CMake toolchain 文件在 .xbuild/zig.cmake，用 zig cc 做交叉编译
- 环境变量 CMAKE_TOOLCHAIN_FILE 已经指向它的绝对路径，任何 cmake configure（包括给依赖单独建的
  子目录）都会自动使用，不需要再传 -DCMAKE_TOOLCHAIN_FILE
- 目标平台由环境变量决定，已经配置好，不要改 toolchain 里的编译器路径

标准流程：
1. 先看项目结构和 CMakeLists.txt，了解构建选项
2. cmake -B build [其他选项]
3. cmake --build build -j4
4. 失败就读报错，判断原因，调整 cmake 选项或打补丁，重来

命令限制（重要）：
- 每次只能执行一条命令，不支持 && || | 和重定向，写了也不会生效
- 可用命令只有 cmake make ninja git ls file nm ldd readelf，没有 cat（读文件用 read_file 工具）
- 只能访问工作目录内的路径，主机上的库和头文件不能用于交叉编译
- 要删除 build 目录用 cmake -E rm -rf build，没有 rm 可用
- 想看文件内容直接用 read_file 工具，不要用 cat 或 git grep 绕
- 编译命令要给足超时，configure 传 180，build 传 300

要点：
- 优先通过 cmake 选项解决，尽量不改项目源码
- 关掉测试、示例、共享库这类非必需目标可以减少麻烦，但不能把所有编译目标都关掉；
  header-only 项目至少保留一个会被编译的测试或示例
- 报错提到某个编译警告被 -Werror 变成错误时，通常是语言标准或警告选项和
  工具链不匹配，试 -DCMAKE_C_STANDARD / -DCMAKE_CXX_STANDARD，或关掉该项目
  自己的严格检查选项
- 同一个办法失败两次就换思路，不要重复
- 每次 configure 或 build 失败后，先用 search_knowledge 查经验库再动手改
- 成功的判据：cmake --build build 返回 exit=0，并且 build 目录里产出了目标架构的
  库、可执行文件或目标文件。没用 toolchain 编出来的主机产物不算

完成后用一句话说明结果；失败就说清楚卡在哪一类问题上。"""


def call_tool(workdir, name, args):
    fn = tool_set()[1].get(name)
    if fn is None:
        return f"错误：不存在名为 {name} 的工具"
    try:
        return fn(workdir, **args)
    except ToolError as e:
        return f"工具失败：{e}"
    except TypeError as e:
        return f"参数错误：{e}"
    except Exception as e:
        return f"未预期的错误：{type(e).__name__}: {e}"


def prepare(source, workspace):
    workspace.mkdir(parents=True, exist_ok=True)
    workspace = workspace.resolve()
    name = source.rstrip("/").split("/")[-1].replace(".git", "")
    project = (workspace / name).resolve()
    if not name or project.parent != workspace:
        raise ValueError(f"无法从 {source} 得到合法的项目目录名")
    if project.exists():
        shutil.rmtree(project)
    if source.startswith(("http://", "https://", "git@")):
        subprocess.run(["git", "clone", "-q", "--depth", "1", source, str(project)], check=True)
    else:
        shutil.copytree(source, project)
    xbuild = project / ".xbuild"
    xbuild.mkdir(exist_ok=True)
    for f in (ROOT / "toolchain").iterdir():
        shutil.copy2(f, xbuild / f.name)
        if not f.suffix:
            (xbuild / f.name).chmod(0o755)
    return project


def survey(project):
    files = sorted(p.name for p in project.iterdir() if not p.name.startswith("."))
    lines = [f"项目根目录: {', '.join(files[:25])}"]
    cml = project / "CMakeLists.txt"
    if cml.exists():
        text = cml.read_text(errors="replace")
        opts = [l.strip() for l in text.splitlines()
                if l.strip().lower().startswith(("option(", "set(cmake_c_standard", "set(cmake_cxx_standard"))]
        lines.append(f"CMakeLists.txt 共 {len(text.splitlines())} 行")
        if opts:
            lines.append("可用的构建选项：")
            lines.extend("  " + o for o in opts[:20])
    return "\n".join(lines)


def commit_of(project):
    if not (project / ".git").exists():
        return None
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project, capture_output=True, text=True)
    return r.stdout.strip() or None


def elf_machines(path):
    with open(path, "rb") as f:
        head = f.read(20)
        if head[:4] == b"\x7fELF" and len(head) == 20:
            return [int.from_bytes(head[18:20], "little" if head[5] == 1 else "big")]
        if head[:8] != b"!<arch>\n":
            return []
        data = head + f.read()
    found, pos = [], 8
    while pos + 60 <= len(data):
        size = int(data[pos + 48:pos + 58].strip() or 0)
        member = data[pos + 60:pos + 80]
        if member[:4] == b"\x7fELF" and len(member) == 20:
            found.append(int.from_bytes(member[18:20], "little" if member[5] == 1 else "big"))
        pos += 60 + size + size % 2
    return found


def scan_artifacts(build_dir, arch):
    want = ELF_MACHINES.get(arch)
    hits, foreign = [], []
    for p in build_dir.rglob("*"):
        if p.is_symlink() or not p.is_file():
            continue
        parts = p.relative_to(build_dir).parts
        if any(a == "CMakeFiles" and (re.fullmatch(r"\d+\.\d+.*", b) or b in ("CMakeScratch", "CMakeTmp"))
               for a, b in zip(parts, parts[1:])):
            continue
        machines = elf_machines(p)
        if not machines:
            continue
        rel = str(p.relative_to(build_dir))
        if want is None or all(m == want for m in machines):
            hits.append(rel)
        else:
            foreign.append(rel)
    return hits, foreign


def verify(project, arch):
    r = subprocess.run(
        ["cmake", "--build", "build", "-j4"],
        cwd=project, capture_output=True, text=True, timeout=600,
    )
    log = (r.stdout + r.stderr)[-3000:]
    if r.returncode != 0:
        return False, log
    hits, foreign = scan_artifacts(project / "build", arch)
    if foreign:
        return False, log + f"\n[验收] 有 {len(foreign)} 个产物不是 {arch} 架构，例如 {foreign[0]}"
    if not hits:
        return False, log + f"\n[验收] build 目录里没有 {arch} 架构的 ELF 产物，等于什么都没交叉编译"
    return True, log + f"\n[验收] 找到 {len(hits)} 个 {arch} 架构产物，例如 {hits[0]}"


def build_events(source, target, arch):
    os.environ["ZIG_TARGET"] = target
    os.environ["ZIG_ARCH"] = arch
    project = prepare(source, ROOT / "workspace")
    os.environ["CMAKE_TOOLCHAIN_FILE"] = str(project / ".xbuild" / "zig.cmake")
    commit = commit_of(project)

    client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com")
    tracer = Trace(ROOT / "runs.db")
    run_id = tracer.start(f"cross-build {project.name} -> {target}", project, MODEL)

    schemas = tool_set()[0]
    survey_text = survey(project)
    yield {"type": "run_started", "run_id": run_id, "project": project.name, "target": target,
           "model": MODEL, "use_kb": USE_KB, "max_steps": MAX_STEPS, "commit": commit,
           "survey": survey_text}

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"把这个项目交叉编译到 {target}。\n\n{survey_text}"},
    ]
    seen = {}
    seen_errors = []
    status = "max_steps"
    step = 0

    try:
        for step in range(1, MAX_STEPS + 1):
            resp = client.chat.completions.create(model=MODEL, messages=messages, tools=schemas)
            tracer.add_usage(resp.usage)
            msg = resp.choices[0].message
            messages.append(msg)

            if msg.content:
                yield {"type": "message", "step": step, "content": msg.content}

            if not msg.tool_calls:
                status = "agent_done"
                break

            for tc in msg.tool_calls:
                started = time.perf_counter()
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                    result = f"参数不是合法 JSON: {tc.function.arguments}"
                    ok = False
                else:
                    key = tc.function.name + json.dumps(args, sort_keys=True)
                    seen[key] = seen.get(key, 0) + 1
                    yield {"type": "tool_call", "step": step, "tool": tc.function.name, "args": args}
                    if seen[key] > REPEAT_LIMIT:
                        result = f"这个调用已经重复 {seen[key]} 次且没有进展，换个思路"
                        ok = False
                    else:
                        result = call_tool(project, tc.function.name, args)
                        ok = not result.startswith(("工具失败：", "参数错误：", "未预期的错误：", "错误："))
                        if tc.function.name == "run_command" and not result.startswith("[exit=0]"):
                            ok = False
                        if tc.function.name == "write_file" and ok:
                            seen = {k: v for k, v in seen.items() if not k.startswith("run_command")}

                elapsed = (time.perf_counter() - started) * 1000
                tracer.tool_call(step, tc.function.name, args, ok, result, elapsed)

                kinds = errors.classify(result)
                seen_errors.extend(kinds)
                yield {"type": "tool_result", "step": step, "tool": tc.function.name, "ok": ok,
                       "result": result[:4000], "ms": round(elapsed), "errors": kinds,
                       "details": errors.extract_details(result, 2) if kinds else []}

                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

            if all(c > REPEAT_LIMIT for c in seen.values()) and len(seen) > 2:
                status = "stuck"
                break
    except Exception as e:
        tracer.finish(f"crash:{type(e).__name__}", step, f"{type(e).__name__}: {e}")
        raise

    yield {"type": "verifying"}
    passed, log = verify(project, arch)
    status = "success" if passed else ("failed" if status == "agent_done" else status)
    tracer.finish(status, step, log[-1000:])

    yield {"type": "finished", "run_id": run_id, "project": project.name, "passed": passed,
           "status": status, "steps": step, "errors": dict(Counter(seen_errors)),
           "commit": commit, "log": log[-3000:]}


def build(source, target, arch):
    summary = None
    for ev in build_events(source, target, arch):
        kind = ev["type"]
        if kind == "run_started":
            print(f"项目: {ev['project']}   目标: {ev['target']}   知识库: {'开' if ev['use_kb'] else '关'}")
            print("=" * 64)
        elif kind == "message":
            print(f"\n[{ev['step']}] 模型: {ev['content'][:200]}")
        elif kind == "tool_call":
            shown = {k: (v[:60] + "...") if isinstance(v, str) and len(v) > 60 else v
                     for k, v in ev["args"].items()}
            print(f"\n[{ev['step']}] {ev['tool']}({shown})")
        elif kind == "tool_result":
            if ev["errors"]:
                print(f"  -> [{ev['ms']}ms] 报错类型: {ev['errors']}")
                for d in ev["details"]:
                    print(f"     {d[:110]}")
            else:
                first = ev["result"].splitlines()[0][:110] if ev["result"] else ""
                print(f"  -> [{ev['ms']}ms] {first}")
        elif kind == "verifying":
            print("\n" + "=" * 64)
        elif kind == "finished":
            summary = ev
            print(f"结果: {'编译通过' if ev['passed'] else '未通过'}   状态: {ev['status']}   步数: {ev['steps']}")
            if ev["errors"]:
                print(f"遇到的报错类型: {ev['errors']}")
            if not ev["passed"]:
                print("最后的构建输出:")
                for line in ev["log"].splitlines()[-6:]:
                    print("   ", line[:120])

    return {"project": summary["project"], "passed": summary["passed"],
            "status": summary["status"], "steps": summary["steps"], "errors": summary["errors"],
            "commit": summary["commit"]}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python build_agent.py <仓库地址或本地路径> [target] [arch]")
        sys.exit(1)
    src = sys.argv[1]
    tgt = sys.argv[2] if len(sys.argv) > 2 else "aarch64-linux-musl"
    ar = sys.argv[3] if len(sys.argv) > 3 else tgt.split("-")[0]
    build(src, tgt, ar)
