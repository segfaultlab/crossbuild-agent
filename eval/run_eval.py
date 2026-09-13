import json
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

import build_agent

ROOT = Path(__file__).resolve().parent.parent
TARGET = "aarch64-linux-musl"
ARCH = "aarch64"


def main(only_tier=None, tag="baseline", projects_file="projects.json"):
    projects = json.loads((ROOT / "eval" / projects_file).read_text())
    if only_tier:
        projects = [p for p in projects if p["tier"] == only_tier]

    results = []
    started = time.time()
    for i, p in enumerate(projects, 1):
        print(f"\n{'#' * 70}\n# [{i}/{len(projects)}] {p['name']}  ({p['tier']})  {p['note']}\n{'#' * 70}")
        t0 = time.time()
        try:
            r = build_agent.build(p["url"], TARGET, ARCH)
        except Exception as e:
            r = {"project": p["name"], "passed": False, "status": f"crash:{type(e).__name__}",
                 "steps": 0, "errors": {}}
            print(f"崩溃: {type(e).__name__}: {e}")
        r.update(tier=p["tier"], note=p["note"], seconds=round(time.time() - t0, 1),
                 model=build_agent.MODEL, use_kb=build_agent.USE_KB, max_steps=build_agent.MAX_STEPS,
                 kb_mode=build_agent.KB_MODE if build_agent.USE_KB else None, impl=build_agent.AGENT_IMPL)
        results.append(r)
        if r["status"].startswith("crash"):
            print(f"保留工作目录以便续跑: {build_agent.WORKSPACE / p['name']}")
        else:
            shutil.rmtree(build_agent.WORKSPACE / p["name"], ignore_errors=True)

    out = ROOT / "eval" / f"result_{tag}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    report(results, tag, time.time() - started)
    return results


def report(results, tag, elapsed):
    n = len(results)
    ok = sum(1 for r in results if r["passed"])
    print(f"\n{'=' * 70}\n{tag} 结果：{ok}/{n} 通过  ({ok / n * 100:.0f}%)   总耗时 {elapsed / 60:.1f} 分钟\n")
    print(f"{'项目':<14}{'档':<6}{'结果':<8}{'步数':<6}{'耗时':<8}报错类型")
    for r in results:
        mark = "通过" if r["passed"] else "失败"
        errs = ",".join(r["errors"]) if r["errors"] else "-"
        print(f"{r['project']:<14}{r['tier']:<6}{mark:<8}{r['steps']:<6}{r['seconds']:<8.0f}{errs}")
    by_tier = Counter(r["tier"] for r in results if r["passed"])
    total_tier = Counter(r["tier"] for r in results)
    print(f"\n按难度：" + "  ".join(f"{t} {by_tier[t]}/{total_tier[t]}" for t in total_tier))
    all_errs = Counter()
    for r in results:
        all_errs.update(r["errors"])
    if all_errs:
        print(f"报错分布：{dict(all_errs)}")
    print(f"平均步数：{sum(r['steps'] for r in results) / n:.1f}")


if __name__ == "__main__":
    tier = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != "all" else None
    tag = sys.argv[2] if len(sys.argv) > 2 else "baseline"
    main(tier, tag, sys.argv[3] if len(sys.argv) > 3 else "projects.json")
