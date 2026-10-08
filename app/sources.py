"""Collector: reads every source in sources.yml and records unseen URLs as candidates.

No LLM here. Strategies:
  rss      RSS/Atom feed (title, summary and date come with the item)
  sitemap  XML sitemap or sitemap index, filtered on URL pattern and <lastmod>
  html     listing page; links matching `link_pattern` (title = anchor text)

Each source has a `gate` applied to title + summary + URL slug:
  ai+edu  must mention AI and education (general news: VRT, Knack, BRUZZ, ...)
  ai      must mention AI (education outlets whose slugs/titles are descriptive)
  none    keep everything (small education outlets); the article text is gated later
`{year}` in a url or pattern is replaced by the current year, and `url_date`
(a regex with three groups) reads the publication date from the URL.

The first time a source is collected, undated items beyond the first
`bootstrap_take` are stored as 'baseline' so an initial scrape does not flood
the pipeline with years of back catalogue.
"""
import html as htmllib
import logging
import os
import re
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin

import feedparser
import httpx
import yaml

import db
import extract
from vocab import normalise_url

log = logging.getLogger("sources")

TASKS_DIR = Path(os.environ.get("TASKS_DIR", "/tasks"))

AI_RE = re.compile(
    r"(?<![a-z0-9])(ai|a\.i\.|genai|gen-ai|artifici[eë]le[ -]intelligentie|artificial[ -]intelligence|"
    r"kunstmatige[ -]intelligentie|chatgpt|chat-gpt|generatieve|chatbots?|taalmodel(?:len)?|llms?|"
    r"copilot|gemini|claude|openai|anthropic|notebooklm|deepfakes?|mistral|perplexity)(?![a-z0-9])",
    re.I)
EDU_RE = re.compile(
    r"(onderwijs|school|scholen|scholier|leerling|leerkracht|leraar|lerares|leraren|docent|"
    r"lesgever|student|universiteit|hogeschool|examen|klas(?:sen)?\b|kleuter|secundair|"
    r"academiejaar|schooljaar|rector|huiswerk|eindtermen|minimumdoelen|lerarenopleiding|"
    r"opleiding|cursus|leerplan|ugent|ku ?leuven|vub|uantwerpen|uhasselt|howest|hogent|"
    r"thomas more|ucll|pxl|artevelde|vives|odisee|ap hogeschool|klasse|digisprong)",
    re.I)


def load_config() -> dict:
    return yaml.safe_load((TASKS_DIR / "sources.yml").read_text(encoding="utf-8"))


def _words(url: str) -> str:
    """URL slug as words, so 'ai-bootcamps-ku-leuven' can pass the keyword gate."""
    return re.sub(r"[-_/]+", " ", url.split("://", 1)[-1])


def passes_feed_gate(gate: str, title: str, summary: str, url: str) -> bool:
    if gate == "none":
        return True
    blob = " ".join((title, summary, _words(url)))
    if not AI_RE.search(blob):
        return False
    return gate == "ai" or bool(EDU_RE.search(blob))


def _subst(text: str) -> str:
    return text.replace("{year}", str(date.today().year))


def _iso(d) -> str:
    return d.strftime("%Y-%m-%d") if d else ""


def _parse_date(s: str) -> date | None:
    if not s:
        return None
    s = s.strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")[:25]).date()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(s).date()
    except (TypeError, ValueError):
        pass
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    return date(int(m[1]), int(m[2]), int(m[3])) if m else None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


# ---------- strategies: each yields dicts {url, title, summary, published, image} ----------
def _rss(src: dict, client: httpx.Client):
    r = extract.get(_subst(src["url"]), client)
    feed = feedparser.parse(r.content)
    for e in feed.entries:
        link = e.get("link") or ""
        if not link:
            continue
        pub = None
        for key in ("published_parsed", "updated_parsed"):
            if e.get(key):
                pub = date(*e[key][:3])
                break
        image = ""
        for m in e.get("media_content", []) + e.get("media_thumbnail", []):
            if str(m.get("url", "")).startswith("https://"):
                image = m["url"]
                break
        yield {"url": link, "title": _clean(e.get("title", "")),
               "summary": _clean(e.get("summary", ""))[:1000], "published": pub, "image": image}


def _sitemap(src: dict, client: httpx.Client, window_start: date):
    pattern = re.compile(_subst(src.get("url_pattern", ".")))
    child_pattern = re.compile(src.get("child_pattern", "."))

    def read(url: str, depth: int = 0):
        xml = extract.get(url, client).text
        if "<sitemapindex" in xml and depth < 2:
            children = re.findall(r"<sitemap>\s*<loc>([^<]+)</loc>(?:\s*<lastmod>([^<]+)</lastmod>)?",
                                  xml)
            for loc, lastmod in children[: src.get("max_children", 10)]:
                loc = htmllib.unescape(loc.strip())
                lm = _parse_date(lastmod)
                if child_pattern.search(loc) and (lm is None or lm >= window_start):
                    yield from read(loc, depth + 1)
            return
        for block in re.findall(r"<url>(.*?)</url>", xml, re.S):
            loc = re.search(r"<loc>([^<]+)</loc>", block)
            if not loc:
                continue
            loc = htmllib.unescape(loc.group(1).strip())
            if not pattern.search(loc):
                continue
            lm = re.search(r"<lastmod>([^<]+)</lastmod>", block)
            title = re.search(r"<news:title>([^<]+)</news:title>", block)
            yield {"url": loc, "title": _clean(title.group(1)) if title else "",
                   "summary": "", "published": _parse_date(lm.group(1)) if lm else None,
                   "image": "", "lastmod_only": True}
    yield from read(_subst(src["url"]))


def _html(src: dict, client: httpx.Client):
    r = extract.get(_subst(src["url"]), client)
    pattern = re.compile(_subst(src["link_pattern"]))
    seen = set()
    for m in re.finditer(r'<a\b[^>]*href="([^"#]+)"[^>]*>(.*?)</a>', r.text, re.S | re.I):
        href = urljoin(str(r.url), htmllib.unescape(m.group(1)))
        if not pattern.search(href) or href in seen:
            continue
        seen.add(href)
        yield {"url": href, "title": _clean(m.group(2))[:300], "summary": "",
               "published": None, "image": ""}


# ---------- collection ----------
def collect_source(src: dict, client: httpx.Client, window_days: int) -> tuple[int, int]:
    """Returns (items offered by the source, new candidates stored)."""
    window_start = date.today() - timedelta(days=window_days)
    kind = src["kind"]
    if kind == "rss":
        items = list(_rss(src, client))
    elif kind == "sitemap":
        items = list(_sitemap(src, client, window_start))
    elif kind == "html":
        items = list(_html(src, client))
    else:
        raise ValueError(f"onbekend brontype {kind!r}")

    state = db.get_source_state(src["id"])
    bootstrapping = not state["bootstrapped"]
    take = int(src.get("bootstrap_take", 10))
    gate = src.get("gate", "none")
    url_date = re.compile(src["url_date"]) if src.get("url_date") else None
    new = 0
    undated_seen = 0
    for it in items:
        url = normalise_url(it["url"])
        if db.candidate_exists(url):
            continue
        pub = it["published"]
        if pub is None and url_date and (m := url_date.search(it["url"])):
            try:
                pub = date(int(m[1]), int(m[2]), int(m[3]))
                it["lastmod_only"] = False
            except ValueError:
                pass
        # Sitemap <lastmod> is a modification date, not a publication date: it
        # can only rule an item out, never date the post.
        published = "" if it.get("lastmod_only") else _iso(pub)
        if pub and pub < window_start:
            continue  # old news; not worth a row
        if not passes_feed_gate(gate, it["title"], it["summary"], it["url"]):
            status, reason = "gated", f"filter '{gate}': geen trefwoord in titel/samenvatting/URL"
        elif bootstrapping and (not pub or it.get("lastmod_only")):
            undated_seen += 1
            status, reason = ("new", "") if undated_seen <= take else (
                "baseline", "bestond al bij de eerste scan van deze bron")
        else:
            status, reason = "new", ""
        if db.insert_candidate(url, it["url"], src["id"], it["title"], it["summary"],
                               published, it["image"], status, reason):
            new += 1 if status == "new" else 0
    return len(items), new


def collect(run_log) -> dict:
    cfg = load_config()
    window_days = int(cfg.get("window_days", 21))
    sources = [s for s in cfg["sources"] if s.get("enabled", True)]
    total_new, failures = 0, []
    with httpx.Client(headers={"User-Agent": extract.USER_AGENT}, follow_redirects=True,
                      timeout=extract.TIMEOUT) as client:
        for src in sources:
            try:
                offered, new = collect_source(src, client, window_days)
                db.set_source_state(src["id"], last_ok=db.now(), last_error="",
                                    last_count=offered, bootstrapped=1)
                total_new += new
                run_log(f"{src['id']}: {offered} items aangeboden, {new} nieuwe kandidaten")
            except Exception as exc:  # noqa: BLE001 — one broken source must not stop the rest
                msg = f"{type(exc).__name__}: {exc}"[:300]
                db.set_source_state(src["id"], last_error=f"{db.now()} {msg}")
                failures.append(src["id"])
                run_log(f"{src['id']}: FOUT {msg}")
    status = "ok" if not failures else ("warning" if len(failures) < len(sources) else "error")
    summary = f"{len(sources)} bronnen gelezen, {total_new} nieuwe kandidaten"
    if failures:
        summary += f"; mislukt: {', '.join(failures)}"
    return {"status": status, "summary": summary, "items": total_new, "commit": "", "cost": 0.0}
