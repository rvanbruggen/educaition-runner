"""SQLite persistence: run history, task state, scraped candidates, source health, settings."""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "runner.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task TEXT NOT NULL,
    started TEXT NOT NULL,          -- UTC ISO
    finished TEXT,                  -- UTC ISO
    trigger TEXT NOT NULL,          -- 'schedule' | 'manual' | 'review'
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
-- Every URL any source ever offered. The normalised URL is the dedupe key, so a
-- story is looked at once, not rediscovered every day.
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,       -- normalised
    raw_url TEXT NOT NULL,
    source TEXT NOT NULL,
    title TEXT DEFAULT '',
    feed_summary TEXT DEFAULT '',
    published TEXT DEFAULT '',      -- YYYY-MM-DD when known
    image TEXT DEFAULT '',
    first_seen TEXT NOT NULL,
    updated TEXT NOT NULL,
    status TEXT NOT NULL,           -- see STATUSES
    reason TEXT DEFAULT '',
    data TEXT DEFAULT '{}'          -- JSON: classification, draft, attempts
);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status);
CREATE INDEX IF NOT EXISTS idx_candidates_source ON candidates(source);
CREATE TABLE IF NOT EXISTS source_state (
    source TEXT PRIMARY KEY,
    last_ok TEXT DEFAULT '',
    last_error TEXT DEFAULT '',
    last_count INTEGER DEFAULT 0,
    bootstrapped INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Candidate lifecycle. Terminal unless noted.
STATUSES = {
    "new": "wacht op verwerking",            # not terminal
    "baseline": "bestond al bij eerste scan",
    "gated": "geen AI×onderwijs-trefwoorden",
    "old": "buiten datumvenster",
    "duplicate": "staat al op de site",
    "fetch_error": "pagina niet op te halen",
    "no_date": "geen publicatiedatum",
    "irrelevant": "niet relevant (LLM)",
    "elsewhere": "relevant, maar niet Vlaams",
    "invalid": "validatie mislukt",
    "ready": "klaar om te publiceren",      # not terminal: review queue / auto-publish
    "published": "gepubliceerd",
    "rejected": "afgewezen in review",
}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)


# ---------- runs ----------
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


def first_run_date() -> str:
    with conn() as c:
        r = c.execute("SELECT MIN(started) AS s FROM runs").fetchone()
        return (r["s"] or "")[:10]


def mark_stale_runs():
    """Runs still 'running' at startup were killed by a restart."""
    with conn() as c:
        c.execute("UPDATE runs SET status='error', finished=?, "
                  "error='Onderbroken door een herstart van de container' "
                  "WHERE status='running'", (now(),))


# ---------- task state ----------
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


# ---------- candidates ----------
def _row(r) -> dict:
    d = dict(r)
    d["data"] = json.loads(d.get("data") or "{}")
    return d


def candidate_exists(url: str) -> bool:
    with conn() as c:
        return c.execute("SELECT 1 FROM candidates WHERE url=?", (url,)).fetchone() is not None


def insert_candidate(url: str, raw_url: str, source: str, title: str, feed_summary: str,
                     published: str, image: str, status: str, reason: str = "") -> bool:
    """Insert if unseen. Returns True when the URL was new."""
    ts = now()
    with conn() as c:
        cur = c.execute(
            """INSERT OR IGNORE INTO candidates
               (url, raw_url, source, title, feed_summary, published, image,
                first_seen, updated, status, reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (url, raw_url, source, title, feed_summary, published, image, ts, ts, status, reason),
        )
        return cur.rowcount == 1


def update_candidate(cid: int, **fields):
    if "data" in fields:
        fields["data"] = json.dumps(fields["data"], ensure_ascii=False)
    fields["updated"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE candidates SET {cols} WHERE id=?", (*fields.values(), cid))


def get_candidate(cid: int) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM candidates WHERE id=?", (cid,)).fetchone()
        return _row(r) if r else None


def candidates_by_status(status: str, limit: int = 200, oldest_first: bool = False) -> list[dict]:
    order = "ASC" if oldest_first else "DESC"
    with conn() as c:
        rows = c.execute(
            f"SELECT * FROM candidates WHERE status=? ORDER BY id {order} LIMIT ?",
            (status, limit)).fetchall()
        return [_row(r) for r in rows]


def candidates_list(status: str | None = None, source: str | None = None,
                    limit: int = 200) -> list[dict]:
    q, args = "SELECT * FROM candidates WHERE 1=1", []
    if status:
        q += " AND status=?"
        args.append(status)
    if source:
        q += " AND source=?"
        args.append(source)
    q += " ORDER BY updated DESC LIMIT ?"
    args.append(limit)
    with conn() as c:
        return [_row(r) for r in c.execute(q, args).fetchall()]


def candidates_by_urls(urls: list[str]) -> dict[str, dict]:
    if not urls:
        return {}
    out = {}
    with conn() as c:
        for i in range(0, len(urls), 500):
            chunk = urls[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for r in c.execute(f"SELECT * FROM candidates WHERE url IN ({marks})", chunk):
                out[r["url"]] = _row(r)
    return out


def status_counts() -> dict[str, int]:
    with conn() as c:
        return {r["status"]: r["n"] for r in
                c.execute("SELECT status, COUNT(*) AS n FROM candidates GROUP BY status")}


def source_yield() -> dict[str, dict[str, int]]:
    """{source: {status: count}} — candidates → relevant → published, per source."""
    out: dict[str, dict[str, int]] = {}
    with conn() as c:
        for r in c.execute(
                "SELECT source, status, COUNT(*) AS n FROM candidates GROUP BY source, status"):
            out.setdefault(r["source"], {})[r["status"]] = r["n"]
    return out


# ---------- source state ----------
def get_source_state(source: str) -> dict:
    with conn() as c:
        r = c.execute("SELECT * FROM source_state WHERE source=?", (source,)).fetchone()
        return dict(r) if r else {"source": source, "last_ok": "", "last_error": "",
                                  "last_count": 0, "bootstrapped": 0}


def all_source_states() -> dict[str, dict]:
    with conn() as c:
        return {r["source"]: dict(r) for r in c.execute("SELECT * FROM source_state")}


def set_source_state(source: str, **fields):
    state = get_source_state(source)
    state.update(fields)
    with conn() as c:
        c.execute(
            """INSERT INTO source_state (source, last_ok, last_error, last_count, bootstrapped)
               VALUES (:source, :last_ok, :last_error, :last_count, :bootstrapped)
               ON CONFLICT(source) DO UPDATE SET last_ok=excluded.last_ok,
                 last_error=excluded.last_error, last_count=excluded.last_count,
                 bootstrapped=excluded.bootstrapped""", state)


# ---------- settings ----------
def get_setting(key: str, default: str = "") -> str:
    with conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


def set_setting(key: str, value: str):
    with conn() as c:
        c.execute("INSERT INTO settings (key, value) VALUES (?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
