import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCRIPT = """
import sys, types, zlib
import numpy as np

class FakeEncoder:
    def __init__(self, name):
        pass

    def encode(self, texts, normalize_embeddings=True):
        out = []
        for t in texts:
            v = np.zeros(16)
            for w in t.lower().split():
                v[zlib.crc32(w.encode()) % 16] += 1
            out.append(v / (np.linalg.norm(v) or 1))
        return np.array(out)

sys.modules["sentence_transformers"] = types.SimpleNamespace(SentenceTransformer=FakeEncoder)
sys.path.insert(0, sys.argv[1])
import knowledge
kb = knowledge.HybridKnowledge(path=knowledge.Path(sys.argv[2]), db_path=knowledge.Path(sys.argv[3]))
print(json.dumps([e["id"] for _, e in kb.search("zlib not found", topk=2)]))
""".replace("import sys, types, zlib", "import json, sys, types, zlib")


class HybridStore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.entries = self.tmp / "entries.jsonl"
        rows = [
            {"id": "a", "title": "zlib not found", "patterns": ["Could NOT find ZLIB"], "cause": "zlib", "fix": "build zlib"},
            {"id": "b", "title": "werror", "patterns": ["-Werror"], "cause": "flags", "fix": "turn off"},
            {"id": "c", "title": "fpic", "patterns": ["recompile with -fPIC"], "cause": "pic", "fix": "pic on"},
        ]
        self.entries.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_once(self):
        r = subprocess.run([sys.executable, "-c", SCRIPT, str(ROOT / "agent"), str(self.entries), str(self.tmp / "m.db")],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_existing_collection_is_searchable_in_new_process(self):
        first = self.run_once()
        second = self.run_once()
        self.assertEqual(first[0], "a")
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
