"""Executes one task and records the result.

Task kinds (tasks.yml `kind`):
  collect   read all sources into the candidates table (no LLM)
  articles  the article pipeline (see articles.py)
  agent     v1: a Claude Agent SDK run of a prompt in tasks/*.md (kept for the
            tasks that have no pipeline yet; `mode: off` while Cowork runs them)
"""
import asyncio
import json
import logging
import os
import threading
from pathlib import Path

import db
import repo

log = logging.getLogger("runner")

LOG_DIR = db.DATA_DIR / "logs"
REPORT_PATH = db.DATA_DIR / "report.json"
TASKS_DIR = Path(os.environ.get("TASKS_DIR", "/tasks"))

# One run at a time — the Sunday chain queues if a run overruns, and nothing
# else touches the repo checkout while a run holds it.
RUN_LOCK = threading.Lock()


class RunLog:
    """Line-based transcript for a run, shown on the run detail page."""

    def __init__(self, path: Path):
        self.path = path

    def __call__(self, line: str):
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(f"[{db.now()}] {line}\n")
        log.info(line)


# ---------- kind: agent (v1) ----------
async def _run_agent(prompt: str, max_turns: int, transcript_path: Path) -> dict:
    """Run the Claude Agent SDK; return {'cost': float, 'is_error': bool, 'result': str}."""
    from claude_agent_sdk import ClaudeAgentOptions, query

    def _stderr(line: str):
        # The SDK only says "Check stderr output for details" — keep the details.
        with open(transcript_path, "a", encoding="utf-8") as fh:
            fh.write(f"[{db.now()}] stderr: {repo._redact(line.rstrip())}\n")

    options = ClaudeAgentOptions(
        stderr=_stderr,
        cwd=str(repo.REPO_DIR),
        model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-5"),
        max_turns=max_turns,
        # The container is an isolated sandbox with only this checkout — safe to skip prompts.
        permission_mode="bypassPermissions",
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep",
                       "WebSearch", "WebFetch", "TodoWrite"],
    )
    outcome = {"cost": 0.0, "is_error": False, "result": ""}
    with open(transcript_path, "a", encoding="utf-8") as fh:
        async for message in query(prompt=prompt, options=options):
            fh.write(f"[{db.now()}] {message!r}\n")
            fh.flush()
            name = type(message).__name__
            if name == "ResultMessage":
                outcome["cost"] = float(getattr(message, "total_cost_usd", 0) or 0)
                outcome["is_error"] = bool(getattr(message, "is_error", False))
                outcome["result"] = str(getattr(message, "result", "") or "")
    return outcome


def _agent_task(task_cfg: dict, transcript: Path) -> dict:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY ontbreekt in .env (nodig voor de agenttaken)")
    REPORT_PATH.unlink(missing_ok=True)
    try:
        repo.sync()
        head_before = repo.head()
        prompt = (TASKS_DIR / task_cfg["prompt"]).read_text(encoding="utf-8")
        outcome = asyncio.run(_run_agent(prompt, int(task_cfg.get("max_turns", 100)), transcript))
        result = {"cost": outcome["cost"], "commit": "", "items": 0}
        if REPORT_PATH.exists():
            report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
            status = report.get("status", "warning")
            result.update(status=status if status in ("ok", "warning", "error") else "warning",
                          summary=str(report.get("summary", ""))[:500],
                          commit=str(report.get("commit", ""))[:40],
                          items=int(report.get("items", 0) or 0))
        else:
            # Agent never wrote its report — infer what we can.
            result.update(status="error" if outcome["is_error"] else "warning",
                          summary="Agent leverde geen report.json af. Laatste output: "
                                  + outcome["result"][:300])
            head_after = repo.head()
            if head_after != head_before:
                result["commit"] = head_after
        return result
    finally:
        REPORT_PATH.unlink(missing_ok=True)


# ---------- dispatch ----------
def _dispatch(task_cfg: dict, run_log: RunLog, transcript: Path) -> dict:
    kind = task_cfg.get("kind", "agent")
    if kind == "collect":
        import sources
        return sources.collect(run_log)
    if kind == "articles":
        import articles
        return articles.run(task_cfg.get("mode", "shadow"), run_log)
    if kind == "agent":
        return _agent_task(task_cfg, transcript)
    raise ValueError(f"onbekend taaktype {kind!r}")


def execute_task(task_id: str, task_cfg: dict, trigger: str = "schedule"):
    """Synchronous entry point used by both the scheduler and the web UI."""
    if trigger == "schedule":
        if task_cfg.get("mode") == "off":
            log.info("Task %s staat op mode=off; geplande run overgeslagen", task_id)
            return
        if not db.is_enabled(task_id):
            log.info("Task %s is gepauzeerd; geplande run overgeslagen", task_id)
            return

    with RUN_LOCK:
        run_id = db.start_run(task_id, db.now(), trigger)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        transcript = LOG_DIR / f"{run_id}-{task_id}.log"
        run_log = RunLog(transcript)
        status, summary, commit, items, cost, error = "error", "", "", 0, 0.0, ""
        try:
            result = _dispatch(task_cfg, run_log, transcript)
            status = result["status"]
            summary = result.get("summary", "")
            commit = result.get("commit", "")
            items = int(result.get("items", 0))
            cost = float(result.get("cost", 0.0))
        except Exception as exc:  # noqa: BLE001 — record any failure, never crash the app
            error = repo._redact(f"{type(exc).__name__}: {exc}")
            summary = summary or "Run mislukt."
            run_log(f"FOUT: {error}")
            log.exception("Run %s (%s) failed", run_id, task_id)
        finally:
            db.finish_run(run_id, db.now(), status, summary, commit, items, cost, error)


def publish_reviewed(cids: list[int], task_id: str) -> tuple[bool, str]:
    """'Publiceer' from the review queue. Returns (ok, message)."""
    import articles
    if not RUN_LOCK.acquire(blocking=False):
        return False, "Er loopt al een run; probeer het zo dadelijk opnieuw."
    try:
        run_id = db.start_run(task_id, db.now(), "review")
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        run_log = RunLog(LOG_DIR / f"{run_id}-{task_id}.log")
        status, summary, commit, error = "error", "", "", ""
        try:
            repo.sync()
            cands = [c for c in (db.get_candidate(i) for i in cids) if c and c["status"] == "ready"]
            commit = articles.publish_candidates(cands, run_log)
            status = "ok" if commit else "warning"
            summary = (f"{len(cands)} artikel(s) gepubliceerd vanuit de review"
                       if commit else "Niets gepubliceerd (intussen al op de site?)")
        except Exception as exc:  # noqa: BLE001
            error = repo._redact(f"{type(exc).__name__}: {exc}")
            summary = "Publiceren vanuit de review mislukt."
            run_log(f"FOUT: {error}")
        finally:
            db.finish_run(run_id, db.now(), status, summary, commit, len(cids), 0.0, error)
        return status == "ok", summary + (f" {error}" if error else "")
    finally:
        RUN_LOCK.release()
