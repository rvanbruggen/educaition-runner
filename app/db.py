"""SQLite persistence for run history and task state."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path("/data/runner.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task TEXT NOT NULL,
    started TEXT NOT NULL,          -- UTC ISO
    finished TEXT,                  -- UTC ISO
    trigger TEXT NOT NULL,          -- 'schedule' | 'manual'
    status TEXT NOT NULL,           -- 'running' | 'ok' | 'warning' | 'error'
    summary TEXT DEFAULT '',
    commit_hash TEXT DEFAULT '',
    items INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    error TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS task_state (
    task TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1
);
"""


@contextmanager
def conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA)


def start_run(task: str, started: str, trigger: str) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO runs (task, started, trigger, status) VALUES (?,?,?,'running')",
            (task, started, trigger),
        )
        return cur.lastrowid


def finish_run(run_id: int, finished: str, status: str, summary: str,
               commit_hash: str, items: int, cost_usd: float, error: str = ""):
    with conn() as c:
        c.execute(
            """UPDATE runs SET finished=?, status=?, summary=?, commit_hash=?,
               items=?, cost_usd=?, error=? WHERE id=?""",
            (finished, status, summary, commit_hash, items, cost_usd, error, run_id),
        )


def recent_runs(limit: int = 50, task: str | None = None):
    with conn() as c:
        if task:
            rows = c.execute(
                "SELECT * FROM runs WHERE task=? ORDER BY id DESC LIMIT ?", (task, limit))
        else:
            rows = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows.fetchall()]


def last_run(task: str):
    rows = recent_runs(limit=1, task=task)
    return rows[0] if rows else None


def get_run(run_id: int):
    with conn() as c:
        r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(r) if r else None


def month_cost() -> float:
    with conn() as c:
        r = c.execute(
            "SELECT COALESCE(SUM(cost_usd),0) AS s FROM runs "
            "WHERE started >= strftime('%Y-%m-01T00:00:00', 'now')").fetchone()
        return r["s"]


def is_enabled(task: str) -> bool:
    with conn() as c:
        r = c.execute("SELECT enabled FROM task_state WHERE task=?", (task,)).fetchone()
        return True if r is None else bool(r["enabled"])


def set_enabled(task: str, enabled: bool):
    with conn() as c:
        c.execute(
            "INSERT INTO task_state (task, enabled) VALUES (?,?) "
            "ON CONFLICT(task) DO UPDATE SET enabled=excluded.enabled",
            (task, int(enabled)),
        )
