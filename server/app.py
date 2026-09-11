import json
import queue
import sqlite3
import sys
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

import build_agent

DB = ROOT / "runs.db"
DIST = ROOT / "web" / "dist"

app = FastAPI(title="CrossBuild Agent")
running = threading.Lock()


def rows(sql, params=()):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def check_source(source):
    if source.startswith(("http://", "https://", "git@")):
        return
    if not (ROOT / source).exists() and not Path(source).exists():
        raise HTTPException(400, f"不是 git 地址，本地也找不到这个路径: {source}")


def stream(source, target, arch):
    q = queue.Queue()

    def worker():
        try:
            for ev in build_agent.build_events(source, target, arch):
                q.put(ev)
        except Exception as e:
            q.put({"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()
    try:
        while True:
            ev = q.get()
            if ev is None:
                break
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
    finally:
        running.release()


@app.get("/api/build")
def api_build(source: str, target: str = "aarch64-linux-musl", arch: str = ""):
    check_source(source)
    if not running.acquire(blocking=False):
        raise HTTPException(409, "已经有一个构建在跑，等它结束再来")
    return StreamingResponse(
        stream(source, target, arch or target.split("-")[0]),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/runs")
def api_runs(limit: int = 50):
    return rows(
        "SELECT id, task, model, started_at, finished_at, status, steps,"
        " prompt_tokens, completion_tokens FROM runs ORDER BY id DESC LIMIT ?",
        (limit,),
    )


@app.get("/api/runs/{run_id}")
def api_run(run_id: int):
    run = rows("SELECT * FROM runs WHERE id = ?", (run_id,))
    if not run:
        raise HTTPException(404, "没有这条运行记录")
    calls = rows(
        "SELECT step, tool, args, ok, result, duration_ms FROM tool_calls"
        " WHERE run_id = ? ORDER BY id",
        (run_id,),
    )
    return {"run": run[0], "calls": calls}


if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/")
    def index():
        return FileResponse(DIST / "index.html")
