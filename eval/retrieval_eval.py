import json
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

import knowledge

MODES = ["bm25", "dense", "hybrid", "hybrid_rerank"]
KINDS = ["原始", "见过的报错", "改写描述", "没见过的变体"]


def evaluate(kb, rows, topk=3):
    per = defaultdict(lambda: {"n": 0, "r1": 0, "r3": 0, "mrr": 0.0})
    misses, elapsed = [], 0.0
    for r in rows:
        gold = r["gold"] if isinstance(r["gold"], list) else [r["gold"]]
        t0 = time.perf_counter()
        ids = [e["id"] for _, e in kb.search(r["q"], topk=topk)]
        elapsed += time.perf_counter() - t0
        rank = next((i + 1 for i, x in enumerate(ids) if x in gold), None)
        for key in (r["kind"], "全部"):
            s = per[key]
            s["n"] += 1
            s["r1"] += rank == 1
            s["r3"] += rank is not None
            s["mrr"] += 1 / rank if rank else 0
        if rank != 1:
            misses.append({"kind": r["kind"], "q": r["q"][:60], "gold": gold, "got": ids})
    return per, misses, elapsed / len(rows) * 1000


def main(modes):
    rows = [json.loads(l) for l in (ROOT / "eval" / "retrieval_testset.jsonl").read_text().splitlines() if l.strip()]
    report = {}
    for mode in modes:
        t0 = time.perf_counter()
        kb = knowledge.load(mode)
        load_s = time.perf_counter() - t0
        kb.search("warmup", topk=3)
        per, misses, ms = evaluate(kb, rows)
        report[mode] = {"load_s": round(load_s, 1), "ms_per_query": round(ms, 1),
                        "metrics": {k: {"n": v["n"], "recall@1": v["r1"], "recall@3": v["r3"],
                                        "mrr": round(v["mrr"] / v["n"], 3)} for k, v in per.items()},
                        "misses": misses}
        print(f"\n== {mode}  加载 {load_s:.1f}s  每条查询 {ms:.1f}ms  条目 {len(kb.entries)}")
        for k in KINDS + ["全部"]:
            v = per[k]
            print(f"  {k:<8} n={v['n']:<3} R@1 {v['r1']:>2}/{v['n']:<3} R@3 {v['r3']:>2}/{v['n']:<3} MRR {v['mrr'] / v['n']:.3f}")
    out = ROOT / "eval" / "result_retrieval.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(sys.argv[1:] or MODES)
