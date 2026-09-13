import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task TEXT NOT NULL,
    workdir TEXT NOT NULL,
    model TEXT,
    started_at REAL NOT NULL,
    finished_at REAL,
    status TEXT,
    steps INTEGER DEFAULT 0,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    result TEXT
);
CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    step INTEGER NOT NULL,
    tool TEXT NOT NULL,
    args TEXT,
    ok INTEGER NOT NULL,
    result TEXT,
    duration_ms REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_run ON tool_calls(run_id);
"""


class Trace:
    def __init__(self, db_path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self.run_id = None

    def start(self, task, workdir, model):
        cur = self.conn.execute(
            "INSERT INTO runs (task, workdir, model, started_at) VALUES (?, ?, ?, ?)",
            (task, str(workdir), model, time.time()),
        )
        self.conn.commit()
        self.run_id = cur.lastrowid
        return self.run_id

    def tool_call(self, step, tool, args, ok, result, duration_ms):
        self.conn.execute(
            "INSERT INTO tool_calls (run_id, step, tool, args, ok, result, duration_ms, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.run_id,
                step,
                tool,
                json.dumps(args, ensure_ascii=False),
                1 if ok else 0,
                result[:2000],
                duration_ms,
                time.time(),
            ),
        )
        self.conn.commit()

    def add_usage(self, usage):
        if not usage:
            return
        self.conn.execute(
            "UPDATE runs SET prompt_tokens = prompt_tokens + ?, completion_tokens = completion_tokens + ?"
            " WHERE id = ?",
            (usage.prompt_tokens or 0, usage.completion_tokens or 0, self.run_id),
        )
        self.conn.commit()

    def finish(self, status, steps, result):
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, steps = ?, result = ? WHERE id = ?",
            (time.time(), status, steps, (result or "")[:4000], self.run_id),
        )
        self.conn.commit()
