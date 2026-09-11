import json
import os
import sys
import time
from pathlib import Path

from openai import OpenAI

from tools import REGISTRY, SCHEMAS, ToolError
from trace import Trace

def load_env(path=Path(__file__).resolve().parent.parent / ".env"):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


load_env()

MODEL = os.environ.get("MODEL", "deepseek-chat")
MAX_STEPS = 10

SYSTEM_PROMPT = """你是一个在本地工作目录里执行任务的助手。
你可以列目录、读文件、执行白名单内的命令。
每次只做一步，根据工具返回的结果决定下一步。
工具失败时不要重复同样的调用，先分析原因再换做法。
任务完成后直接给出结论，不要再调用工具。"""


def call_tool(workdir: Path, name: str, args: dict) -> str:
    fn = REGISTRY.get(name)
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


def run(task: str, workdir: Path) -> str:
    client = OpenAI(
        api_key=os.environ["DEEPSEEK_API_KEY"],
        base_url="https://api.deepseek.com",
    )
    tracer = Trace(Path(__file__).resolve().parent.parent / "runs.db")
    run_id = tracer.start(task, workdir, MODEL)
    print(f"run #{run_id}")
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]

    for step in range(1, MAX_STEPS + 1):
        resp = client.chat.completions.create(model=MODEL, messages=messages, tools=SCHEMAS)
        tracer.add_usage(resp.usage)
        msg = resp.choices[0].message
        messages.append(msg)

        if not msg.tool_calls:
            print(f"\n[第 {step} 步] 结束")
            tracer.finish("done", step, msg.content)
            return msg.content or ""

        for tc in msg.tool_calls:
            started = time.perf_counter()
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}
                result = f"参数不是合法 JSON: {tc.function.arguments}"
            else:
                print(f"\n[第 {step} 步] {tc.function.name}({args})")
                result = call_tool(workdir, tc.function.name, args)
            elapsed = (time.perf_counter() - started) * 1000
            ok = not result.startswith(("工具失败：", "参数错误：", "未预期的错误：", "错误：", "参数不是合法 JSON"))
            tracer.tool_call(step, tc.function.name, args, ok, result, elapsed)
            preview = result if len(result) < 300 else result[:300] + " ..."
            print(f"  -> {preview}")
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    tracer.finish("max_steps", MAX_STEPS, None)
    return f"达到最大步数 {MAX_STEPS}，未完成"


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python mini_agent.py '任务描述' [工作目录]")
        sys.exit(1)
    task = sys.argv[1]
    workdir = Path(sys.argv[2] if len(sys.argv) > 2 else ".").resolve()
    print(f"任务: {task}\n工作目录: {workdir}")
    print("=" * 60)
    print("\n" + "=" * 60)
    print(run(task, workdir))
