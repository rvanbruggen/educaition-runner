"""EducAItion Taakbeheer — scheduler + web UI in one FastAPI app."""
import base64
import logging
import os
import re
import secrets
import threading
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

import db
import llm
import repo
import runner
import schedule
import sources
import vocab
from version import __version__

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

TASKS: dict = yaml.safe_load((runner.TASKS_DIR / "tasks.yml").read_text(encoding="utf-8"))["tasks"]
for _cfg in TASKS.values():
    # YAML 1.1 reads an unquoted `mode: off` as False; the code compares to "off".
    if _cfg.get("mode") is False:
        _cfg["mode"] = "off"
TZ = os.environ.get("TZ", "Europe/Brussels")
ARTICLE_TASK = next((k for k, v in TASKS.items() if v.get("kind") == "articles"), None)

app = FastAPI(title="EducAItion Taakbeheer")
jinja = Environment(loader=FileSystemLoader(os.path.join(os.path.dirname(__file__), "templates")),
                    autoescape=select_autoescape(["html"]))
jinja.globals["version"] = __version__
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")),
          name="static")

scheduler = BackgroundScheduler(timezone=TZ)


def render(template: str, request: Request | None = None, **ctx) -> HTMLResponse:
    ctx.setdefault("review_count", db.status_counts().get("ready", 0))
    if request is not None:
        ctx.setdefault("flash", request.query_params.get("msg", ""))
    return HTMLResponse(jinja.get_template(template).render(**ctx))


def back(path: str, msg: str = "") -> RedirectResponse:
    return RedirectResponse(path + (f"?msg={quote(msg)}" if msg else ""), status_code=303)


# ---------- auth (optional HTTP Basic) ----------
@app.middleware("http")
async def basic_auth(request: Request, call_next):
    password = os.environ.get("ADMIN_PASSWORD", "")
    if password and request.url.path != "/health":
        header = request.headers.get("authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                ok = user == "rik" and secrets.compare_digest(pw, password)
            except Exception:  # noqa: BLE001
                ok = False
        if not ok:
            return Response(status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="EducAItion"'})
    return await call_next(request)


# ---------- scheduling ----------
def _job(task_id: str):
    runner.execute_task(task_id, TASKS[task_id], trigger="schedule")


def _cron_for(task_id: str) -> str:
    """Effective cron: the override saved from the dashboard, else tasks.yml."""
    return db.get_setting(f"cron_{task_id}") or TASKS[task_id]["cron"]


@app.on_event("startup")
def startup():
    db.init()
    db.mark_stale_runs()
    for task_id in TASKS:
        cron = _cron_for(task_id)
        try:
            trigger = CronTrigger.from_crontab(cron, timezone=TZ)
        except ValueError:
            log.warning("Ongeldige cron %r voor %s; terug naar tasks.yml", cron, task_id)
            db.delete_setting(f"cron_{task_id}")
            trigger = CronTrigger.from_crontab(TASKS[task_id]["cron"], timezone=TZ)
        scheduler.add_job(_job, trigger, args=[task_id], id=task_id, max_instances=1,
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
        mode = cfg.get("mode", "auto")
        overdue = False
        if last and last["finished"] and enabled and mode != "off":
            hours = (now - datetime.strptime(last["started"], "%Y-%m-%dT%H:%M:%SZ")
                     .replace(tzinfo=timezone.utc)).total_seconds() / 3600
            overdue = hours > cfg["interval_hours"] + cfg["grace_hours"]
        job = scheduler.get_job(task_id)
        next_run = "—"
        if mode != "off" and job and job.next_run_time:
            next_run = job.next_run_time.strftime("%a %d %b %H:%M")
        cron = _cron_for(task_id)
        custom = cron != cfg["cron"]
        label = (f"{schedule.describe(cron)} (aangepast; standaard {cfg['schedule_label']})"
                 if custom else cfg["schedule_label"])
        picker = schedule.parse(cron) or {"freq": "daily", "every": 2, "hour": 5,
                                          "minute": 0, "days": ["sun"]}
        out.append({
            "id": task_id, "name": cfg["name"], "schedule": label,
            "cron": cron, "cron_custom": custom, "default_cron": cfg["cron"],
            "picker": picker, "picker_ok": schedule.parse(cron) is not None,
            "enabled": enabled, "last": last, "overdue": overdue, "mode": mode,
            "running": bool(last and last["status"] == "running"),
            "next_run": next_run,
        })
    return out


# ---------- routes: tasks ----------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return render("dashboard.html", request, active="dash",
                  tasks=_task_view(), runs=db.recent_runs(30), tz=TZ,
                  days=schedule.DAYS, every_choices=schedule.EVERY_CHOICES,
                  month_cost=db.month_cost(), task_names={k: v["name"] for k, v in TASKS.items()})


@app.post("/run/{task_id}")
def run_now(task_id: str):
    if task_id not in TASKS:
        raise HTTPException(404)
    if TASKS[task_id].get("mode") == "off":
        return back("/", "Deze taak staat op mode=off.")
    threading.Thread(target=runner.execute_task,
                     args=(task_id, TASKS[task_id]), kwargs={"trigger": "manual"},
                     daemon=True).start()
    return back("/")


@app.post("/schedule/{task_id}")
async def schedule_save(task_id: str, request: Request):
    """Change a task's cron from the dashboard; takes effect without a restart."""
    if task_id not in TASKS:
        raise HTTPException(404)
    form = await request.form()
    cron = " ".join(str(form.get("cron", "")).split())   # "Geavanceerd": raw cron wins
    if not cron and not form.get("reset"):
        try:
            cron = schedule.build(str(form.get("freq", "")), str(form.get("every", "")),
                                  str(form.get("time", "")), form.getlist("days"),
                                  minute=str(form.get("minute", "")))
        except ValueError as exc:
            return back("/", f"Schema van {TASKS[task_id]['name']} niet bewaard: {exc}")
    if form.get("reset") or cron == TASKS[task_id]["cron"]:
        db.delete_setting(f"cron_{task_id}")
        cron = TASKS[task_id]["cron"]
        msg = f"Schema van {TASKS[task_id]['name']} terug op standaard ({cron})."
    else:
        try:
            CronTrigger.from_crontab(cron, timezone=TZ)
        except ValueError as exc:
            return back("/", f"Ongeldige cron '{cron}': {exc}. "
                             "Formaat: minuut uur dag maand weekdag (bv. 8 5 * * * of 7 18 * * sun).")
        db.set_setting(f"cron_{task_id}", cron)
        msg = f"Schema van {TASKS[task_id]['name']} bewaard: {schedule.describe(cron)}."
    scheduler.reschedule_job(task_id, trigger=CronTrigger.from_crontab(cron, timezone=TZ))
    return back("/", msg)


@app.post("/toggle/{task_id}")
def toggle(task_id: str):
    if task_id not in TASKS:
        raise HTTPException(404)
    db.set_enabled(task_id, not db.is_enabled(task_id))
    return back("/")


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: int):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404)
    transcript = ""
    path = runner.LOG_DIR / f"{run_id}-{run['task']}.log"
    if path.exists():
        transcript = path.read_text(encoding="utf-8", errors="replace")[-40000:]
    return render("run.html", request, run=run, transcript=transcript,
                  task_name=TASKS.get(run["task"], {}).get("name", run["task"]))


# ---------- routes: review queue ----------
@app.get("/review", response_class=HTMLResponse)
def review(request: Request):
    mode = TASKS.get(ARTICLE_TASK, {}).get("mode", "shadow")
    return render("review.html", request, active="review", mode=mode,
                  items=db.candidates_by_status("ready", 200, oldest_first=True))


@app.post("/review/{cid}/publish")
def review_publish(cid: int):
    ok, msg = runner.publish_reviewed([cid], ARTICLE_TASK or "artikelen-vlaanderen")
    return back("/review", msg)


@app.post("/review/{cid}/reject")
def review_reject(cid: int):
    c = db.get_candidate(cid)
    if not c:
        raise HTTPException(404)
    if c["status"] == "ready":
        db.update_candidate(cid, status="rejected", reason="afgewezen in review")
    return back("/review", "Afgewezen.")


# ---------- routes: candidates & sources ----------
@app.get("/candidates", response_class=HTMLResponse)
def candidates(request: Request, status: str = "", source: str = ""):
    return render("candidates.html", request, active="cand", status=status, source=source,
                  statuses=db.STATUSES, counts=db.status_counts(),
                  items=db.candidates_list(status or None, source or None, 300))


@app.get("/candidates/{cid}", response_class=HTMLResponse)
def candidate(request: Request, cid: int):
    c = db.get_candidate(cid)
    if not c:
        raise HTTPException(404)
    markdown = repo.render_article(c["data"]["post"]) if c["data"].get("post") else ""
    return render("candidate.html", request, active="cand", c=c, markdown=markdown,
                  statuses=db.STATUSES)


@app.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request):
    cfg = sources.load_config()
    states = db.all_source_states()
    yields = db.source_yield()
    rows = []
    for s in cfg["sources"]:
        st = states.get(s["id"], {})
        y = yields.get(s["id"], {})
        rows.append({
            "id": s["id"], "kind": s["kind"], "gate": s.get("gate", "none"),
            "enabled": s.get("enabled", True),
            "last_ok": st.get("last_ok", ""), "error": st.get("last_error", ""),
            "last_count": st.get("last_count", 0),
            "total": sum(n for k, n in y.items() if k not in ("baseline", "gated")),
            "relevant": sum(y.get(k, 0) for k in ("ready", "published", "rejected")),
            "published": y.get("published", 0),
        })
    return render("sources.html", request, active="src", rows=rows)


@app.get("/compare", response_class=HTMLResponse)
def compare(request: Request, since: str = ""):
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", since or ""):
        since = db.first_run_date() or (date.today() - timedelta(days=14)).isoformat()
    site_rows, found = [], 0
    domains = {urlsplit(s["url"]).netloc.removeprefix("www.")
               for s in sources.load_config()["sources"]}
    if (repo.REPO_DIR / ".git").exists():
        out = repo.git("log", f"--since={since}", "--diff-filter=A", "--name-only",
                       "--pretty=format:", "--", "_posts", check=False).stdout
        files = sorted({f for f in out.splitlines() if f.endswith(".md")}, reverse=True)
        posts = []
        for f in files:
            p = repo.REPO_DIR / f
            if not p.exists():
                continue
            text = p.read_text(encoding="utf-8")
            t = re.search(r"^type:\s*(\S+)", text, re.M)
            b = re.search(r"^bron:\s*[\"']?(\S+?)[\"']?\s*$", text, re.M)
            if not t or t.group(1) not in vocab.ARTICLE_TYPES or not b:
                continue  # digests, media and academic posts belong to other tasks
            posts.append((f.removeprefix("_posts/"), b.group(1)))
        cands = db.candidates_by_urls([vocab.normalise_url(b) for _, b in posts])
        for f, bron in posts:
            cand = cands.get(vocab.normalise_url(bron))
            domain = urlsplit(bron).netloc.removeprefix("www.")
            found += 1 if cand else 0
            site_rows.append({"file": f, "bron": bron, "domain": domain, "cand": cand,
                              "known_domain": any(domain.endswith(d) or d.endswith(domain)
                                                  for d in domains)})
    # What the runner published itself shows up in site_rows; this is what it has
    # that the site does not (yet).
    site_urls = {vocab.normalise_url(r["bron"]) for r in site_rows}
    runner_only = [c for st in ("ready", "rejected") for c in db.candidates_by_status(st, 300)
                   if c["url"] not in site_urls and c["updated"][:10] >= since]
    return render("compare.html", request, active="cmp", since=since, site_rows=site_rows,
                  found=found, runner_only=runner_only)


# ---------- routes: settings ----------
@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request):
    specs = {s: ":".join(llm.get_spec(s)) for s in llm.STEPS}
    keys = {s: bool(os.environ.get("OPENAI_API_KEY" if llm.get_spec(s)[0] == "openai"
                                   else "ANTHROPIC_API_KEY")) for s in llm.STEPS}
    return render("settings.html", request, active="set", steps=llm.STEPS, specs=specs,
                  keys=keys, prices=llm.PRICES)


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    bad = []
    for step in llm.STEPS:
        spec = str(form.get(step, "")).strip()
        provider, _, model = spec.partition(":")
        if provider in llm.PROVIDERS and re.match(r"^[A-Za-z0-9._-]+$", model or ""):
            db.set_setting(f"llm_{step}", spec)
        else:
            bad.append(step)
    msg = "Bewaard." if not bad else f"Ongeldig formaat voor: {', '.join(bad)} (gebruik provider:model)."
    return back("/settings", msg)


@app.get("/health", response_class=PlainTextResponse)
def health():
    return f"ok {__version__}"
