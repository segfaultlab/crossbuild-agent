import os
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

import build_agent as core
import errors
from trace import Trace

CHECKPOINT_DB = core.ROOT / "checkpoints.db"


class State(TypedDict):
    messages: Annotated[list, add_messages]
    project: str
    target: str
    arch: str
    run_id: int
    step: int
    seen: dict
    errors: list
    status: str
    kb_injected: list
    passed: bool
    log: str


def make_model():
    llm = ChatOpenAI(model=core.MODEL, api_key=os.environ["DEEPSEEK_API_KEY"],
                     base_url="https://api.deepseek.com")
    return llm.bind_tools(core.tool_set()[0])


def build_graph(model, tracer, checkpointer=None):
    def agent(state: State):
        step = state["step"] + 1
        msg = model.invoke(state["messages"])
        usage = msg.usage_metadata
        if usage:
            tracer.add_usage(SimpleNamespace(prompt_tokens=usage["input_tokens"],
                                             completion_tokens=usage["output_tokens"]))
        if msg.content:
            get_stream_writer()({"type": "message", "step": step, "content": msg.content})
        update = {"messages": [msg], "step": step}
        if not msg.tool_calls and not msg.invalid_tool_calls:
            update["status"] = "agent_done"
        return update

    def tools(state: State):
        write = get_stream_writer()
        project, step = Path(state["project"]), state["step"]
        seen, found, replies = dict(state["seen"]), list(state["errors"]), []
        injected = list(state.get("kb_injected") or [])
        last = state["messages"][-1]
        calls = [(tc["id"], tc["name"], tc["args"], None) for tc in last.tool_calls]
        calls += [(tc["id"], tc["name"], {}, f"参数不是合法 JSON: {tc['args']}") for tc in last.invalid_tool_calls]
        for call_id, name, args, invalid in calls:
            started = time.perf_counter()
            if invalid:
                result, ok = invalid, False
            else:
                write({"type": "tool_call", "step": step, "tool": name, "args": args})
                result, ok = core.check_and_run(project, name, args, seen)
            elapsed = (time.perf_counter() - started) * 1000
            tracer.tool_call(step, name, args, ok, result, elapsed)
            kinds = errors.classify(result)
            found.extend(kinds)
            write({"type": "tool_result", "step": step, "tool": name, "ok": ok, "result": result[:4000],
                   "ms": round(elapsed), "errors": kinds,
                   "details": errors.extract_details(result, 2) if kinds else []})
            auto = core.auto_knowledge(name, ok, result, injected)
            if auto:
                query, ids, note = auto
                result = f"{result}\n\n{note}"
                tracer.tool_call(step, "auto_knowledge", {"query": query[:500]}, True, note, 0)
                write({"type": "kb_inject", "step": step, "ids": ids, "content": note})
            replies.append(ToolMessage(result, tool_call_id=call_id))
        update = {"messages": replies, "seen": seen, "errors": found, "kb_injected": injected}
        if all(c > core.REPEAT_LIMIT for c in seen.values()) and len(seen) > 2:
            update["status"] = "stuck"
        return update

    def verify(state: State):
        get_stream_writer()({"type": "verifying"})
        passed, log = core.verify(Path(state["project"]), state["arch"])
        if passed:
            status = "success"
        elif state["status"] == "agent_done":
            status = "failed"
        else:
            status = state["status"] or "max_steps"
        return {"passed": passed, "log": log, "status": status}

    def after_agent(state: State):
        return "verify" if state["status"] == "agent_done" else "tools"

    def after_tools(state: State):
        return "verify" if state["status"] == "stuck" or state["step"] >= core.MAX_STEPS else "agent"

    g = StateGraph(State)
    g.add_node("agent", agent)
    g.add_node("tools", tools)
    g.add_node("verify", verify)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", after_agent, ["tools", "verify"])
    g.add_conditional_edges("tools", after_tools, ["agent", "verify"])
    g.add_edge("verify", END)
    return g.compile(checkpointer=checkpointer)


def run_config(thread_id):
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": core.MAX_STEPS * 2 + 10}


def build_events(source, target, arch, resume=None, model=None, tracer=None, checkpoint_db=CHECKPOINT_DB):
    tracer = tracer or Trace(core.ROOT / "runs.db")
    conn = sqlite3.connect(checkpoint_db, check_same_thread=False)
    graph = build_graph(model or make_model(), tracer, SqliteSaver(conn))

    if resume:
        config = run_config(resume)
        values = graph.get_state(config).values
        if not values:
            raise ValueError(f"找不到检查点 {resume}")
        project, target, arch = Path(values["project"]), values["target"], values["arch"]
        tracer.run_id = values["run_id"]
        inputs = None
    else:
        project = core.prepare(source, core.WORKSPACE)
        run_id = tracer.start(f"cross-build {project.name} -> {target} [langgraph]", project, core.MODEL)
        config = run_config(f"run-{run_id}")
        inputs = {
            "messages": [SystemMessage(core.SYSTEM_PROMPT),
                         HumanMessage(f"把这个项目交叉编译到 {target}。\n\n{core.survey(project)}")],
            "project": str(project), "target": target, "arch": arch, "run_id": run_id,
            "step": 0, "seen": {}, "errors": [], "status": "", "kb_injected": [], "passed": False, "log": "",
        }

    os.environ["ZIG_TARGET"] = target
    os.environ["ZIG_ARCH"] = arch
    os.environ["CMAKE_TOOLCHAIN_FILE"] = str(project / ".xbuild" / "zig.cmake")
    commit = core.commit_of(project)
    thread_id = config["configurable"]["thread_id"]

    yield {"type": "run_started", "run_id": tracer.run_id, "project": project.name, "target": target,
           "model": core.MODEL, "use_kb": core.USE_KB, "kb_auto": core.KB_AUTO, "max_steps": core.MAX_STEPS,
           "commit": commit, "impl": "langgraph", "thread_id": thread_id, "resumed": bool(resume),
           "survey": core.survey(project)}
    try:
        for chunk in graph.stream(inputs, config, stream_mode="custom"):
            yield chunk
        final = graph.get_state(config).values
    except Exception as e:
        step = graph.get_state(config).values.get("step", 0)
        tracer.finish(f"crash:{type(e).__name__}", step, f"{type(e).__name__}: {e}  可用 --resume {thread_id} 继续")
        conn.close()
        raise
    conn.close()
    tracer.finish(final["status"], final["step"], final["log"][-1000:])
    yield {"type": "finished", "run_id": tracer.run_id, "project": project.name, "passed": final["passed"],
           "status": final["status"], "steps": final["step"], "errors": dict(Counter(final["errors"])),
           "kb_injected": final.get("kb_injected") or [], "commit": commit, "impl": "langgraph", "thread_id": thread_id, "log": final["log"][-3000:]}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python graph_agent.py <仓库地址或本地路径> [target] [arch]\n"
              "      python graph_agent.py --resume <thread_id>")
        sys.exit(1)
    if sys.argv[1] == "--resume":
        core.build(None, None, None, events=build_events(None, None, None, resume=sys.argv[2]))
    else:
        src = sys.argv[1]
        tgt = sys.argv[2] if len(sys.argv) > 2 else "aarch64-linux-musl"
        ar = sys.argv[3] if len(sys.argv) > 3 else tgt.split("-")[0]
        core.build(src, tgt, ar, events=build_events(src, tgt, ar))
