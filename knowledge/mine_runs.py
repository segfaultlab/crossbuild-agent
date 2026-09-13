import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

import errors
from mini_agent import load_env

load_env()

FOLLOW = 8
NOISE = re.compile(r"CMake Error at|Configuring incomplete|Call Stack|^make|ninja: build stopped|\*\*\*|error generated")


def signature(line):
    s = re.sub(r"/[\w./+-]+", "<path>", line)
    s = re.sub(r"\d+", "N", s)
    s = re.sub(r"'[^']*'", "'X'", s)
    return s[:140]


def short(tool, args):
    a = json.loads(args or "{}")
    if tool == "run_command":
        return a.get("command", "")[:220]
    if tool == "write_file":
        return f"write_file {a.get('path')}: {a.get('content', '')[:160]!r}"
    if tool == "search_knowledge":
        return f"search_knowledge {a.get('query', '')[:120]!r}"
    return f"{tool} {json.dumps(a, ensure_ascii=False)[:120]}"


def outcome(result):
    m = re.match(r"\[exit=(\d+)\]", result or "")
    if m:
        return "ok" if m.group(1) == "0" else f"exit={m.group(1)}"
    return "blocked" if (result or "").startswith("工具失败") else "ok"


def episodes(db):
    runs = {r[0]: r for r in db.execute(
        "SELECT id, task, status FROM runs WHERE task LIKE 'cross-build%'")}
    calls = defaultdict(list)
    for row in db.execute("SELECT run_id, step, tool, args, result FROM tool_calls ORDER BY id"):
        if row[0] in runs:
            calls[row[0]].append(row)

    groups = defaultdict(list)
    for run_id, seq in calls.items():
        project = runs[run_id][1].split()[1]
        for i, (_, step, tool, args, result) in enumerate(seq):
            if tool != "run_command" or outcome(result) in ("ok", "blocked"):
                continue
            lines = [d for d in errors.extract_details(result, 6) if not NOISE.search(d)]
            if not lines:
                continue
            follow = [f"[{outcome(r)}] {short(t, a)}" for _, _, t, a, r in seq[i + 1:i + 1 + FOLLOW]]
            groups[signature(lines[0])].append({
                "run": run_id, "project": project, "run_status": runs[run_id][2], "step": step,
                "command": short(tool, args), "errors": lines, "next_actions": follow,
            })
    return groups


def trajectories(db, project, limit=3):
    out = []
    for run_id, status, steps in db.execute(
            "SELECT id, status, steps FROM runs WHERE task LIKE ? ORDER BY id DESC",
            (f"cross-build {project} %",)):
        seq = db.execute("SELECT tool, args, result FROM tool_calls WHERE run_id=? ORDER BY id", (run_id,))
        actions = [f"[{outcome(r)}] {short(t, a)}" for t, a, r in seq if t != "read_file"]
        out.append({"run": run_id, "status": status, "steps": steps, "actions": actions})
        if len(out) >= limit:
            break
    return out


PROMPT = """你在整理一个 C/C++ 交叉编译 Agent 的经验库。Agent 用 zig cc 把开源项目交叉编译到 aarch64-linux-musl，
只能用 cmake/make/ninja/git/ls/file/nm/readelf 等命令，只能访问项目目录。

下面是从真实运行记录里提取的材料：
1. 报错片段：同一类报错出现的次数、所在项目、报错原文、Agent 随后的动作和每个动作的结果
2. 困难项目的完整动作轨迹（成功和失败的都有）

已有经验库条目（不要重复，已覆盖的问题不要再写）：
{existing}

请从材料里总结**新的**经验条目，要求：
- 只写材料里有证据的内容，不要补充材料里没出现过的问题
- 每条要能泛化到别的项目，不要写成"libpng 要这样做"的项目专属配方；项目名可以作为例子出现
- fix 写清楚具体怎么做（命令、CMake 变量、判断顺序），以及哪些做法在材料里被证明是弯路
- patterns 放能在报错或症状里直接匹配到的原文片段；症状类条目（不是报错，而是低效做法）patterns 可以写症状关键词
- source_runs 列出支撑这条经验的 run id

只输出 JSON 数组，每个元素字段：title, patterns, cause, fix, source_runs。

材料：
{material}
"""


def main():
    db = sqlite3.connect(ROOT / "runs.db")
    groups = episodes(db)
    material = {"报错片段": [], "困难项目轨迹": {}}
    for sig, eps in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        picked = {}
        for e in eps:
            picked.setdefault(e["project"], e)
        material["报错片段"].append({"signature": sig, "count": len(eps),
                                  "projects": sorted({e["project"] for e in eps}),
                                  "examples": list(picked.values())[:2]})
    for project in ("libpng", "re2"):
        material["困难项目轨迹"][project] = trajectories(db, project)

    existing = [json.loads(l) for l in (ROOT / "knowledge" / "entries.jsonl").read_text().splitlines() if l.strip()]
    existing_brief = "\n".join(f"- {e['id']} {e['title']}：{e['fix'][:80]}" for e in existing)

    out_dir = ROOT / "knowledge" / "mined"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "material.json").write_text(json.dumps(material, ensure_ascii=False, indent=1))

    from openai import OpenAI
    client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com")
    resp = client.chat.completions.create(
        model=os.environ.get("MODEL", "deepseek-flash"),
        messages=[{"role": "user", "content": PROMPT.format(
            existing=existing_brief, material=json.dumps(material, ensure_ascii=False))}],
    )
    text = resp.choices[0].message.content
    m = re.search(r"\[.*\]", text, re.S)
    candidates = json.loads(m.group(0))
    (out_dir / "candidates.json").write_text(json.dumps(candidates, ensure_ascii=False, indent=1))
    print(f"报错类型 {len(groups)}，候选条目 {len(candidates)}，输入 token {resp.usage.prompt_tokens}")


if __name__ == "__main__":
    main()
