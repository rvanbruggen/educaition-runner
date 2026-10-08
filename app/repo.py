"""The site checkout: sync, post rendering + validation, commit and push.

Callers hold runner.RUN_LOCK around anything that touches the checkout.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

import db
import vocab

REPO_DIR = db.DATA_DIR / "repo"

# Abbreviations with an internal period break the site's excerpt, which is cut at
# the first ". " (EXCERPT RULE in the Cowork prompts).
ABBREV_RE = re.compile(
    r"(?<![A-Za-z])(o\.a|bv|b\.v|dr|prof|m\.a\.w|e\.d|enz|i\.p\.v|o\.m|vb|ca|mr|dhr|mevr|nl|resp|evt)\.",
    re.I)


class PushError(RuntimeError):
    pass


def _redact(text: str) -> str:
    token = os.environ.get("GITHUB_TOKEN", "")
    return text.replace(token, "***") if token else text


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(["git", "-C", str(REPO_DIR), *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        # Never let the tokenised remote URL reach a log or the UI.
        raise RuntimeError(_redact(f"git {args[0]} mislukt: {p.stderr.strip() or p.stdout.strip()}"))
    return p


def remote_url() -> str:
    """SITE_REPO_URL overrides (local testing); otherwise GitHub with the token."""
    if os.environ.get("SITE_REPO_URL"):
        return os.environ["SITE_REPO_URL"]
    token = os.environ["GITHUB_TOKEN"]
    repo = os.environ["GITHUB_REPO"]
    return f"https://x-access-token:{token}@github.com/{repo}.git"


def sync():
    """Clone or hard-reset the site repo to origin/main with an authenticated remote."""
    url = remote_url()
    if not (REPO_DIR / ".git").exists():
        REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
        p = subprocess.run(["git", "clone", url, str(REPO_DIR)], capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(_redact(f"git clone mislukt: {p.stderr.strip()}"))
    else:
        git("remote", "set-url", "origin", url)
        git("fetch", "origin")
        git("reset", "--hard", "origin/main")
        git("clean", "-fd", "--exclude=claude-dashboard")
    git("config", "user.name", os.environ.get("GIT_USER_NAME", "EducAItion Runner"))
    git("config", "user.email", os.environ.get("GIT_USER_EMAIL", "runner@educaition.today"))


def head() -> str:
    return git("rev-parse", "HEAD").stdout.strip()


# ---------------------------------------------------------------------------
# Post rendering
# ---------------------------------------------------------------------------
def _q(s: str) -> str:
    """A YAML double-quoted scalar (JSON string syntax is valid YAML)."""
    return json.dumps(s, ensure_ascii=False)


def _flow(items: list[str]) -> str:
    return "[" + ", ".join(items) + "]"


def render_article(post: dict) -> str:
    """Same layout as the posts the Cowork task writes."""
    lines = ["---",
             f"title: {_q(post['title'])}",
             f"date: {post['date']}",
             f"niveau: {_flow(post['niveau'])}",
             f"vak: {_flow(post['vak'])}",
             f"thema: {_flow(post['thema'])}",
             f"regio: {_flow(post['regio'])}",
             f"type: {post['type']}",
             f"doelgroep: {_flow(post['doelgroep'])}",
             f"bron: {post['bron']}"]
    if post.get("image"):
        lines.append(f"image: {_q(post['image'])}")
    lines += ["---", "", post["body"].strip(), "",
              f"Lees meer bij [{post['bronnaam']}]({post['bron']}).", ""]
    return "\n".join(lines)


def first_sentence(body: str) -> str:
    m = re.search(r"\. ", body)
    return body[: m.start() + 1] if m else body


def validate_article(post: dict, text: str, tags: dict[str, list[str]]) -> list[str]:
    """Hard gate. Returns a list of problems; empty means publishable."""
    problems = []
    try:
        fm_raw = text.split("---\n", 2)[1]
        fm = yaml.safe_load(fm_raw)
    except Exception as exc:  # noqa: BLE001
        return [f"front matter is geen geldige YAML: {exc}"]
    if not isinstance(fm, dict):
        return ["front matter is geen mapping"]
    if str(fm.get("date")) != post["date"]:
        problems.append("datum in front matter klopt niet")
    for facet in ("niveau", "vak", "thema", "regio", "doelgroep"):
        vals = fm.get(facet) or []
        if not isinstance(vals, list) or not vals:
            problems.append(f"{facet} is leeg")
            continue
        bad = [v for v in vals if v not in tags[facet]]
        if bad:
            problems.append(f"onbekende {facet}-tags: {', '.join(map(str, bad))}")
    if fm.get("type") not in tags["type"] or fm.get("type") not in vocab.ARTICLE_TYPES:
        problems.append(f"ongeldig type: {fm.get('type')}")
    if not str(fm.get("bron", "")).startswith("https://"):
        problems.append("bron begint niet met https://")
    if "image" in fm and not str(fm["image"]).startswith("https://"):
        problems.append("image begint niet met https://")
    body = post["body"].strip()
    if "{%" in body or "{{" in body:
        problems.append("Liquid-tags in de tekst (interne links zijn niet toegestaan)")
    if re.search(r"\]\((?!https://)", body):
        problems.append("link zonder https:// in de tekst")
    first = first_sentence(body)
    if len(first) < 40:
        problems.append("eerste zin is te kort voor een excerpt")
    if ABBREV_RE.search(first):
        problems.append("eerste zin bevat een afkorting met punt (excerpt-regel)")
    sentences = len(re.findall(r"[.!?](\s|$)", body))
    if not 2 <= sentences <= 6:
        problems.append(f"tekst heeft {sentences} zinnen (verwacht 2-4)")
    return problems


def post_path(date: str, prefix: str, slug: str) -> Path:
    """_posts/<date>-<prefix>-<slug>.md, unique in the checkout."""
    slug = re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-")[:60].strip("-") or "artikel"
    if not slug.startswith(prefix + "-"):
        slug = f"{prefix}-{slug}"
    base = REPO_DIR / "_posts" / f"{date}-{slug}"
    path, n = Path(f"{base}.md"), 2
    while path.exists():
        path, n = Path(f"{base}-{n}.md"), n + 1
    return path


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------
def publish(files: dict[str, str], message: str, run_log) -> str:
    """Write {relative path: content}, add tag pages, commit and push. Returns the commit hash.

    Expects a freshly synced checkout. On a failed push the local commit is left
    for the next sync to reset; callers keep their items queued so they are retried.
    """
    for rel, content in files.items():
        p = REPO_DIR / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    # Every niveau slug needs a /niveau/<slug>/ page or the check-links workflow fails.
    script = REPO_DIR / "scripts" / "genereer-tagpaginas.py"
    if script.exists():
        p = subprocess.run([sys.executable, str(script)], cwd=REPO_DIR, capture_output=True,
                           text=True)
        if p.returncode != 0:
            raise RuntimeError(f"genereer-tagpaginas.py mislukt: {p.stderr.strip()[:300]}")
    git("add", *files.keys())
    if (REPO_DIR / "niveau").exists():
        git("add", "niveau")
    if not git("diff", "--cached", "--name-only").stdout.strip():
        raise RuntimeError("niets te committen")
    git("commit", "-m", message)
    commit = head()
    p = git("push", "origin", "HEAD:main", check=False)
    if p.returncode != 0:
        raise PushError(_redact(f"push mislukt (commit {commit[:7]} niet op GitHub): "
                                f"{p.stderr.strip()[:400]}"))
    run_log(f"Gepusht: {commit[:7]} ({len(files)} bestand(en))")
    return commit
