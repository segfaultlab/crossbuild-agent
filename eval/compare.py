import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(tag):
    p = ROOT / "eval" / f"result_{tag}.json"
    if not p.exists():
        sys.exit(f"没有 {p}")
    return {r["project"]: r for r in json.loads(p.read_text())}


def main(a_tag, b_tag):
    a, b = load(a_tag), load(b_tag)
    names = [n for n in a if n in b]

    print(f"{'项目':<13}{'档':<6}{a_tag:<18}{b_tag:<18}变化")
    print("-" * 70)
    flips = {"救回": [], "退化": [], "不变": []}
    for n in names:
        ra, rb = a[n], b[n]
        sa = ("通过 %d步" % ra["steps"]) if ra["passed"] else ("失败 %s" % ra["status"])
        sb = ("通过 %d步" % rb["steps"]) if rb["passed"] else ("失败 %s" % rb["status"])
        if not ra["passed"] and rb["passed"]:
            mark, key = "← 救回", "救回"
        elif ra["passed"] and not rb["passed"]:
            mark, key = "← 退化", "退化"
        elif ra["passed"] and rb["passed"]:
            d = rb["steps"] - ra["steps"]
            mark = f"步数 {d:+d}" if d else "持平"
            key = "不变"
        else:
            mark, key = "都失败", "不变"
        flips[key].append(n)
        print(f"{n:<13}{ra['tier']:<6}{sa:<18}{sb:<18}{mark}")

    oa = sum(1 for n in names if a[n]["passed"])
    ob = sum(1 for n in names if b[n]["passed"])
    print("-" * 70)
    print(f"通过率  {a_tag}: {oa}/{len(names)} ({oa/len(names)*100:.0f}%)"
          f"   →   {b_tag}: {ob}/{len(names)} ({ob/len(names)*100:.0f}%)")
    sa_ = sum(a[n]["steps"] for n in names) / len(names)
    sb_ = sum(b[n]["steps"] for n in names) / len(names)
    print(f"平均步数  {sa_:.1f}  →  {sb_:.1f}")
    if flips["救回"]:
        print(f"被救回的：{', '.join(flips['救回'])}")
    if flips["退化"]:
        print(f"退化的：{', '.join(flips['退化'])}")

    print("\n失败原因分布：")
    for tag, d in ((a_tag, a), (b_tag, b)):
        errs = Counter()
        for n in names:
            if not d[n]["passed"]:
                errs.update(d[n]["errors"])
        print(f"  {tag}: {dict(errs) if errs else '无失败'}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "baseline",
         sys.argv[2] if len(sys.argv) > 2 else "rag")
