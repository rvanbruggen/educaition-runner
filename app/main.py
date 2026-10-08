"""EducAItion Taakbeheer — scheduler + web UI in one FastAPI app."""
import logging
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

import db
import runner

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

TASKS: dict = yaml.safe_load((Path("/tasks") / "tasks.yml").read_text(encoding="utf-8"))["tasks"]
TZ = os.environ.get("TZ", "Europe/Brussels")

app = FastAPI(title="EducAItion Taakbeheer")
jinja = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"),
                    autoescape=select_autoescape(["html"]))

scheduler = BackgroundScheduler(timezone=TZ)


# ---------- auth (optional HTTP Basic) ----------
@app.middleware("http")
async def basic_auth(request: Request, call_next):
    password = os.environ.get("ADMIN_PASSWORD", "")
    if password:
        import base64
        header = request.headers.get("authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                ok = user == "rik" and secrets.compare_digest(pw, password)
            except Exception:  # noqa: BLE001
                ok = False
        if not ok:
            from fastapi.responses import Response
            return Response(status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="EducAItion"'})
    return await call_next(request)


# ---------- scheduling ----------
def _job(task_id: str):
    runner.execute_task(task_id, TASKS[task_id], trigger="schedule")


@app.on_event("startup")
def startup():
    db.init()
    for task_id, cfg in TASKS.items():
        scheduler.add_job(_job, CronTrigger.from_crontab(cfg["cron"], timezone=TZ),
                          args=[task_id], id=task_id, max_instances=1,
                          misfire_grace_time=3600, coalesce=True)
    scheduler.start()
    log.info("Scheduler gestart met %d taken (TZ=%s)", len(TASKS), TZ)


@app.on_event("shutdown")
def shutdown():
    scheduler.shutdown(wait=False)


# ---------- helpers ----------
def _task_view():
    now = datetime.now(timezone.utc)
    out = []
    for task_id, cfg in TASKS.items():
        last = db.last_run(task_id)
        enabled = db.is_enabled(task_id)
        overdue = False
        if last and last["finished"] and enabled:
            hours = (now - datetime.strptime(last["started"], "%Y-%m-%dT%H:%M:%SZ")
                     .replace(tzinfo=timezone.utc)).total_seconds() / 3600
            overdue = hours > cfg["interval_hours"] + cfg["grace_hours"]
        job = scheduler.get_job(task_id)
        out.append({
            "id": task_id, "name": cfg["name"], "schedule": cfg["schedule_label"],
            "enabled": enabled, "last": last, "overdue": overdue,
            "running": bool(last and last["status"] == "running"),
            "next_run": job.next_run_time.strftime("%a %d %b %H:%M") if job and job.next_run_time else "—",
        })
    return out


# ---------- routes ----------
@app.get("/", response_class=HTMLResponse)
def dashboard():
    return jinja.get_template("dashboard.html").render(
        tasks=_task_view(), runs=db.recent_runs(30),
        month_cost=db.month_cost(), task_names={k: v["name"] for k, v in TASKS.items()})


@app.post("/run/{task_id}")
def run_now(task_id: str):
    if task_id not in TASKS:
        raise HTTPException(404)
    threading.Thread(target=runner.execute_task,
                     args=(task_id, TASKS[task_id]), kwargs={"trigger": "manual"},
                     daemon=True).start()
    return RedirectResponse("/", status_code=303)


@app.post("/toggle/{task_id}")
def toggle(task_id: str):
    if task_id not in TASKS:
        raise HTTPException(404)
    db.set_enabled(task_id, not db.is_enabled(task_id))
    return RedirectResponse("/", status_code=303)


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(run_id: int):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404)
    transcript = ""
    path = runner.LOG_DIR / f"{run_id}-{run['task']}.log"
    if path.exists():
        transcript = path.read_text(encoding="utf-8", errors="replace")[-40000:]
    return jinja.get_template("run.html").render(
        run=run, transcript=transcript,
        task_name=TASKS.get(run["task"], {}).get("name", run["task"]))


@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"
