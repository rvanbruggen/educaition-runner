"""Executes one task: sync the site repo, run the Claude agent, record the result."""
import asyncio
import json
import logging
import os
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

import db

log = logging.getLogger("runner")

DATA = Path("/data")
REPO_DIR = DATA / "repo"
LOG_DIR = DATA / "logs"
REPORT_PATH = DATA / "report.json"
TASKS_DIR = Path("/tasks")

# One run at a time — the Sunday chain (18:07 / 18:30 / 19:20) queues if a run overruns.
RUN_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(REPO_DIR), *args],
                          capture_output=True, text=True, check=check)


def sync_repo():
    """Clone or hard-reset the site repo to origin/main with an authenticated remote."""
    token = os.environ["GITHUB_TOKEN"]
    repo = os.environ["GITHUB_REPO"]
    url = f"https://x-access-token:{token}@github.com/{repo}.git"
    if not (REPO_DIR / ".git").exists():
        REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", url, str(REPO_DIR)],
                       capture_output=True, text=True, check=True)
    else:
        _git("remote", "set-url", "origin", url)
        _git("fetch", "origin")
        _git("reset", "--hard", "origin/main")
        _git("clean", "-fd", "--exclude=claude-dashboard")
    _git("config", "user.name", os.environ.get("GIT_USER_NAME", "EducAItion Runner"))
    _git("config", "user.email", os.environ.get("GIT_USER_EMAIL", "runner@educaition.today"))


async def _run_agent(prompt: str, max_turns: int, transcript_path: Path) -> dict:
    """Run the Claude Agent SDK; return {'cost': float, 'is_error': bool, 'result': str}."""
    from claude_agent_sdk import query, ClaudeAgentOptions

    options = ClaudeAgentOptions(
        cwd=str(REPO_DIR),
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
            fh.write(f"[{_now()}] {message!r}\n")
            fh.flush()
            name = type(message).__name__
            if name == "ResultMessage":
                outcome["cost"] = float(getattr(message, "total_cost_usd", 0) or 0)
                outcome["is_error"] = bool(getattr(message, "is_error", False))
                outcome["result"] = str(getattr(message, "result", "") or "")
    return outcome


def execute_task(task_id: str, task_cfg: dict, trigger: str = "schedule"):
    """Synchronous entry point used by both the scheduler and the web UI."""
    if not db.is_enabled(task_id) and trigger == "schedule":
        log.info("Task %s is disabled; skipping scheduled run", task_id)
        return

    with RUN_LOCK:
        run_id = db.start_run(task_id, _now(), trigger)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        transcript = LOG_DIR / f"{run_id}-{task_id}.log"
        status, summary, commit, items, cost, error = "error", "", "", 0, 0.0, ""
        try:
            REPORT_PATH.unlink(missing_ok=True)
            sync_repo()
            head_before = _git("rev-parse", "HEAD").stdout.strip()

            prompt = (TASKS_DIR / task_cfg["prompt"]).read_text(encoding="utf-8")
            outcome = asyncio.run(_run_agent(prompt, int(task_cfg.get("max_turns", 100)),
                                             transcript))
            cost = outcome["cost"]

            if REPORT_PATH.exists():
                report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
                status = report.get("status", "warning")
                if status not in ("ok", "warning", "error"):
                    status = "warning"
                summary = str(report.get("summary", ""))[:500]
                commit = str(report.get("commit", ""))[:40]
                items = int(report.get("items", 0) or 0)
            else:
                # Agent never wrote its report — infer what we can.
                status = "error" if outcome["is_error"] else "warning"
                summary = ("Agent leverde geen report.json af. Laatste output: "
                           + outcome["result"][:300])
                head_after = _git("rev-parse", "HEAD").stdout.strip()
                if head_after != head_before:
                    commit = head_after
        except Exception as exc:  # noqa: BLE001 — record any failure, never crash the app
            error = f"{type(exc).__name__}: {exc}"
            summary = summary or "Run mislukt vóór of tijdens de agent-uitvoering."
            log.exception("Run %s (%s) failed", run_id, task_id)
        finally:
            REPORT_PATH.unlink(missing_ok=True)
            db.finish_run(run_id, _now(), status, summary, commit, items, cost, error)
