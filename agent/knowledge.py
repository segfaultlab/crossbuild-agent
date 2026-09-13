import hashlib
import json
import math
import os
import re
from collections import Counter
from pathlib import Path

ENTRIES_PATH = Path(__file__).resolve().parent.parent / "knowledge" / "entries.jsonl"
MILVUS_PATH = ENTRIES_PATH.parent / "milvus.db"
EMBED_MODEL = os.environ.get("EMBED_MODEL", "BAAI/bge-m3")
RERANK_MODEL = os.environ.get("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
RRF_K = 60


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

    def ranked(self, query):
        q = tokenize(query)
        scored = sorted(((self._score(q, i), i) for i in range(len(self.entries))), reverse=True)
        return [i for s, i in scored if s > 0]

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


class DenseKnowledge(Knowledge):
    def __init__(self, path=ENTRIES_PATH, db_path=MILVUS_PATH, model=EMBED_MODEL):
        super().__init__(path)
        from pymilvus import MilvusClient
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(model)
        self.client = MilvusClient(str(db_path))
        texts = [self._text(e) for e in self.entries]
        digest = hashlib.sha1("\n".join([model] + texts).encode()).hexdigest()[:12]
        self.collection = f"kb_{digest}"
        if not self.client.has_collection(self.collection):
            for name in self.client.list_collections():
                if name.startswith("kb_"):
                    self.client.drop_collection(name)
            vectors = self.model.encode(texts, normalize_embeddings=True)
            self.client.create_collection(self.collection, dimension=len(vectors[0]), metric_type="COSINE")
            self.client.insert(self.collection, [{"id": i, "vector": v.tolist()} for i, v in enumerate(vectors)])
        self.client.load_collection(self.collection)

    def dense_ranked(self, query, limit):
        vec = self.model.encode([query], normalize_embeddings=True)[0].tolist()
        hits = self.client.search(self.collection, data=[vec], limit=limit)[0]
        return [(h["distance"], h["id"]) for h in hits]

    def search(self, query, topk=3):
        return [(round(s, 3), self.entries[i]) for s, i in self.dense_ranked(query, topk)]


class HybridKnowledge(DenseKnowledge):
    def __init__(self, rerank=False, pool=10, **kwargs):
        super().__init__(**kwargs)
        self.pool = pool
        self.reranker = None
        if rerank:
            from sentence_transformers import CrossEncoder

            self.reranker = CrossEncoder(RERANK_MODEL)

    def search(self, query, topk=3):
        fused = Counter()
        for rank, i in enumerate(self.ranked(query)[:self.pool]):
            fused[i] += 1 / (RRF_K + rank + 1)
        for rank, (_, i) in enumerate(self.dense_ranked(query, self.pool)):
            fused[i] += 1 / (RRF_K + rank + 1)
        candidates = [i for i, _ in fused.most_common()]
        if self.reranker is None:
            return [(round(fused[i] * 100, 2), self.entries[i]) for i in candidates[:topk]]
        scores = self.reranker.predict([(query, self._text(self.entries[i])) for i in candidates])
        ordered = sorted(zip(scores, candidates), key=lambda x: -x[0])[:topk]
        return [(round(float(sc), 3), self.entries[i]) for sc, i in ordered]


def load(mode="bm25"):
    if mode == "bm25":
        return Knowledge()
    if mode == "dense":
        return DenseKnowledge()
    if mode == "hybrid":
        return HybridKnowledge()
    if mode == "hybrid_rerank":
        return HybridKnowledge(rerank=True)
    raise ValueError(f"未知的 KB_MODE: {mode}")
