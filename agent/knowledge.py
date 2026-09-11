import json
import math
import re
from collections import Counter
from pathlib import Path

ENTRIES_PATH = Path(__file__).resolve().parent.parent / "knowledge" / "entries.jsonl"


def tokenize(s):
    return re.findall(r"[a-zA-Z_][a-zA-Z0-9_+\-.]*|[一-鿿]", s.lower())


class Knowledge:
    def __init__(self, path=ENTRIES_PATH):
        self.entries = []
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("//"):
                    self.entries.append(json.loads(line))
        self.docs = [tokenize(self._text(e)) for e in self.entries]
        self.N = max(len(self.docs), 1)
        self.avgdl = (sum(len(d) for d in self.docs) / self.N) if self.docs else 1
        self.df = Counter()
        for d in self.docs:
            for w in set(d):
                self.df[w] += 1

    @staticmethod
    def _text(e):
        return " ".join([e.get("title", ""), " ".join(e.get("patterns", [])),
                         e.get("cause", ""), e.get("fix", "")])

    def _score(self, query_tokens, i, k1=1.5, b=0.75):
        freq = Counter(self.docs[i])
        s = 0.0
        for w in query_tokens:
            if w not in freq:
                continue
            idf = math.log((self.N - self.df[w] + 0.5) / (self.df[w] + 0.5) + 1)
            tf = freq[w]
            s += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(self.docs[i]) / self.avgdl))
        return s

    def search(self, query, topk=3, min_score=1.0):
        if not self.entries:
            return []
        q = tokenize(query)
        scored = sorted(((self._score(q, i), i) for i in range(len(self.entries))), reverse=True)
        return [(round(s, 2), self.entries[i]) for s, i in scored[:topk] if s >= min_score]

    def format(self, hits):
        if not hits:
            return "知识库里没有匹配的条目，按你自己的判断处理。"
        out = []
        for score, e in hits:
            out.append(
                f"[{e['id']}] {e['title']}（相关度 {score}）\n"
                f"  成因：{e['cause']}\n"
                f"  处理：{e['fix']}"
            )
        return "\n\n".join(out)
