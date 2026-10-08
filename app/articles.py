"""Article scan (replaces the Cowork task educaition-artikelen-vlaanderen).

For each candidate the collector stored as 'new':
  dedupe against the site → fetch + extract → date window → keyword gate on the
  article text → LLM classify (relevance + tags from tags.yml) → LLM write
  (title + 2-4 sentences) → render + validate in code → 'ready'.

mode 'shadow': ready items wait in the review queue (/review) for a manual
publish. mode 'auto': up to MAX_POSTS ready items are committed and pushed at
the end of the run.
"""
import os
import re
from collections import Counter
from datetime import date, timedelta

import db
import extract
import llm
import repo
import sources
import vocab

MAX_PROCESS = 80   # candidates looked at per run (fetching is cheap, LLM calls are capped below)
MAX_LLM = 40       # candidates sent to the LLM per run
MAX_POSTS = 6      # posts published per run (Cowork: 0-6)
MAX_ATTEMPTS = 3
TEXT_LIMIT = 8000  # characters of article text sent to the LLM

SYSTEM = (
    "Je bent redactieassistent van EducAItion (https://www.educaition.today), de "
    "Nederlandstalige website van Rik Van Bruggen over AI in het onderwijs in Vlaanderen. "
    "Je werkt uitsluitend met de tekst die je krijgt en verzint nooit feiten, namen, cijfers "
    "of data.")

CLASSIFY_PROMPT = """Beoordeel of dit artikel thuishoort in de artikelbibliotheek van EducAItion.

RELEVANT als AI (generatieve AI, chatbots, AI-tools, taalmodellen, algoritmes, AI-geletterdheid,
AI-regelgeving) in onderwijs of leren een hoofdonderwerp is of een substantieel deel van het artikel:
leerplichtonderwijs, hoger onderwijs, volwassenenonderwijs, lerarenopleiding, onderwijsbeleid,
evaluatie en examens, studiekeuze.
NIET RELEVANT: AI-nieuws zonder onderwijshoek; onderwijsnieuws waarin AI enkel terloops vermeld
wordt; commerciële cursussen of bedrijfsopleidingen voor professionals; vacatures; agenda-items
zonder inhoud; lesmateriaal zonder AI-onderwerp; AI-onderzoek van een universiteit dat niets met
onderwijs te maken heeft.

region: "vlaanderen" als het gaat over Vlaams of Brussels onderwijs, Vlaamse actoren (scholen,
universiteiten, hogescholen, koepels, minister, Vlaams Parlement) of als een Vlaams medium een
ontwikkeling duidelijk voor een Vlaams publiek kadert; "belgie-federaal" voor federaal Belgisch
beleid; "elders" voor buitenlandse ontwikkelingen zonder Vlaamse of Belgische invalshoek.

Tags (alleen uit de toegelaten waarden):
- type: nieuws (nieuwsbericht), onderzoek (resultaten van een studie of bevraging), opinie
  (opiniestuk, column, interview met standpunten), gids-handleiding (praktische gids), beleidsdocument
  (officiële richtlijn, visietekst, regelgeving), best-practice (concreet voorbeeld uit een school of
  opleiding), casestudy, tool-review.
- niveau: de onderwijsniveaus waarover het gaat (hoofdniveau, plus een subniveau als dat duidelijk is).
- vak: "vakoverschrijdend", tenzij het artikel over een specifiek vak gaat (dan ook dat vak).
- thema: 1 tot 3 thema's.
- doelgroep: wie dit artikel vooral moet lezen.
- onderwijsnet: alleen invullen als een specifiek net centraal staat (go = GO!, katholiek-onderwijs,
  ovsg, pov); anders leeg.
- extra_regio: "internationaal", "eu" of "nederland" als het artikel ook daarover gaat; anders leeg.
reason: één korte Nederlandse zin die je beslissing uitlegt.

duplicate_of: als een van de recente posts hieronder over HETZELFDE nieuwsfeit of dezelfde
publicatie gaat (ook als een ander medium het bracht), geef dan de code van die post; anders "".
Een ander artikel over hetzelfde bredere thema is geen duplicaat.

Recente posts op de site (code: titel):
{recent}

Bron: {source}
URL: {url}
Datum: {date}
Titel: {title}

Tekst:
{text}"""

WRITE_PROMPT = """Schrijf de post voor de artikelbibliotheek van EducAItion over dit artikel.

- title: Nederlandstalige titel, dicht bij de oorspronkelijke kop en zonder clickbait. Is de
  oorspronkelijke titel Nederlands en informatief, neem hem dan over.
- body: 2 tot 4 feitelijke zinnen in toegankelijk Belgisch-Nederlands die het artikel samenvatten
  voor leerkrachten en directies (wie, wat, waarom het ertoe doet). Uitsluitend feiten uit de tekst
  hieronder. Eén alinea, geen links, geen opsommingen, geen "Lees meer".
  De EERSTE ZIN verschijnt los als fragment op de overzichtspagina's: maak hem volledig en
  informatief op zichzelf, en gebruik er GEEN afkortingen met een punt in (schrijf "onder andere",
  "bijvoorbeeld", "professor", "dokter" voluit).
- slug: 3 tot 7 Nederlandse woorden in kebab-case die het onderwerp beschrijven, zonder bronnaam
  en zonder datum (bijvoorbeeld "go-lokeren-ai-taalapp").
{feedback}
Bron: {source}
URL: {url}
Datum: {date}
Oorspronkelijke titel: {title}

Tekst:
{text}"""


def _arr(values: list[str]) -> dict:
    return {"type": "array", "items": {"type": "string", "enum": values}}


def classify_schema(tags: dict[str, list[str]], recent_codes: list[str]) -> dict:
    return {
        "type": "object", "additionalProperties": False,
        "required": ["relevant", "reason", "duplicate_of", "region", "type", "niveau", "vak",
                     "thema", "doelgroep", "onderwijsnet", "extra_regio"],
        "properties": {
            "relevant": {"type": "boolean"},
            "reason": {"type": "string"},
            "duplicate_of": {"type": "string", "enum": [""] + recent_codes},
            "region": {"type": "string", "enum": ["vlaanderen", "belgie-federaal", "elders"]},
            "type": {"type": "string", "enum": vocab.ARTICLE_TYPES},
            "niveau": _arr(tags["niveau"]),
            "vak": _arr(tags["vak"]),
            "thema": _arr(tags["thema"]),
            "doelgroep": _arr(tags["doelgroep"]),
            "onderwijsnet": _arr(vocab.ONDERWIJSNETTEN),
            "extra_regio": _arr(["internationaal", "eu", "nederland"]),
        },
    }


WRITE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "body", "slug"],
    "properties": {"title": {"type": "string"}, "body": {"type": "string"},
                   "slug": {"type": "string"}},
}


def _uniq(items) -> list[str]:
    out = []
    for i in items:
        if i not in out:
            out.append(i)
    return out


def _check_llm_config():
    for step in ("classify", "write"):
        provider, _ = llm.get_spec(step)
        key = "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"
        if not os.environ.get(key):
            raise RuntimeError(f"{key} ontbreekt in .env (nodig voor stap '{step}')")


RECENT_DAYS = 45


def recent_posts() -> dict[str, str]:
    """{filename stem: title} of article posts from the last RECENT_DAYS, for story dedupe."""
    since = (date.today() - timedelta(days=RECENT_DAYS)).isoformat()
    out = {}
    for p in sorted((repo.REPO_DIR / "_posts").glob("*.md"), reverse=True):
        if p.name[:10] < since:
            break
        text = p.read_text(encoding="utf-8")
        t = re.search(r"^type:\s*(\S+)", text, re.M)
        title = re.search(r"^title:\s*(.+)$", text, re.M)
        if t and t.group(1) in vocab.ARTICLE_TYPES and title:
            out[p.stem] = title.group(1).strip().strip('"\'')
    return out


class Pipeline:
    def __init__(self, run_log):
        self.log = run_log
        self.tags = vocab.load(repo.REPO_DIR)
        self.existing = vocab.existing_bron_urls(repo.REPO_DIR)
        cfg = sources.load_config()
        self.sources = {s["id"]: s for s in cfg["sources"]}
        self.window_start = date.today() - timedelta(days=int(cfg.get("window_days", 21)))
        self.recent = recent_posts()
        self.cost = 0.0
        self.llm_calls = 0
        self.counts: Counter = Counter()
        self.llm_errors = 0

    def _set(self, c: dict, status: str, reason: str = "", **fields):
        self.counts[status] += 1
        db.update_candidate(c["id"], status=status, reason=reason[:500], **fields)

    # ---------- one candidate ----------
    def process(self, c: dict):
        src = self.sources.get(c["source"], {"name": c["source"], "prefix": c["source"],
                                              "gate": "none"})
        data = c["data"]
        if c["url"] in self.existing:
            return self._set(c, "duplicate", "bron-URL staat al in _posts")
        try:
            page = extract.fetch_page(c["raw_url"])
        except Exception as exc:  # noqa: BLE001
            data["attempts"] = data.get("attempts", 0) + 1
            status = "fetch_error" if data["attempts"] >= MAX_ATTEMPTS else "new"
            self.counts["fetch_retry" if status == "new" else "fetch_error"] += 1
            db.update_candidate(c["id"], status=status, data=data,
                                reason=f"{type(exc).__name__}: {exc}"[:300])
            return
        if vocab.normalise_url(page.url) in self.existing:
            return self._set(c, "duplicate", "bron-URL (na redirect) staat al in _posts")

        published = c["published"] or page.date
        if not published:
            return self._set(c, "no_date", "geen publicatiedatum in feed of pagina")
        if published < self.window_start.isoformat():
            return self._set(c, "old", f"gepubliceerd op {published}", published=published)

        # Listing-page anchor text is noisy (category labels, teasers); the page's own
        # title is better there. Feed titles are clean.
        if src.get("kind") == "html" and page.title:
            title = page.title
        else:
            title = c["title"] or page.title
        text = page.text or c["feed_summary"]
        blob = f"{title}\n{text}"
        if not sources.AI_RE.search(blob):
            return self._set(c, "gated", "geen AI-trefwoord in de artikeltekst",
                             published=published, title=title)
        if src.get("gate") == "ai+edu" and not sources.EDU_RE.search(blob):
            return self._set(c, "gated", "geen onderwijstrefwoord in de artikeltekst",
                             published=published, title=title)

        if self.llm_calls >= MAX_LLM:
            self.counts["deferred"] += 1
            return  # stays 'new' for the next run
        fields = {"source": src["name"], "url": page.url, "date": published, "title": title,
                  "text": text[:TEXT_LIMIT],
                  "recent": "\n".join(f"{k}: {v}" for k, v in self.recent.items()) or "(geen)"}
        try:
            self.llm_calls += 1
            cls = llm.complete_json("classify", SYSTEM, CLASSIFY_PROMPT.format(**fields),
                                    classify_schema(self.tags, list(self.recent)),
                                    max_tokens=800)
            self.cost += cls.cost
            k = cls.data
            data.update(classification=k, classify_model=cls.model)
            if not k.get("relevant"):
                return self._set(c, "irrelevant", k.get("reason", ""), published=published,
                                 title=title, data=data)
            if k.get("duplicate_of") in self.recent:
                return self._set(c, "duplicate",
                                 f"zelfde nieuws als {k['duplicate_of']}: {k.get('reason', '')}",
                                 published=published, title=title, data=data)
            if k.get("region") == "elders":
                return self._set(c, "elsewhere", k.get("reason", ""), published=published,
                                 title=title, data=data)

            post, problems = None, []
            for attempt in range(2):
                feedback = ""
                if problems:
                    feedback = ("\nDe vorige versie werd afgekeurd: " + "; ".join(problems)
                                + ". Los dat op.\n")
                w = llm.complete_json("write", SYSTEM, WRITE_PROMPT.format(feedback=feedback,
                                                                           **fields),
                                      WRITE_SCHEMA, max_tokens=900)
                self.cost += w.cost
                post = self._post(c, src, page, published, k, w.data)
                problems = repo.validate_article(post, repo.render_article(post), self.tags)
                data["write_model"] = w.model
                if not problems:
                    break
        except Exception as exc:  # noqa: BLE001
            self.llm_errors += 1
            data["attempts"] = data.get("attempts", 0) + 1
            status = "invalid" if data["attempts"] >= MAX_ATTEMPTS else "new"
            db.update_candidate(c["id"], status=status, data=data,
                                reason=f"LLM-fout: {type(exc).__name__}: {exc}"[:300])
            self.log(f"  LLM-fout bij {c['url']}: {exc}")
            return

        data["post"] = post
        if problems:
            data["problems"] = problems
            return self._set(c, "invalid", "; ".join(problems), published=published,
                             title=title, data=data)
        self.recent[f"nieuw-{c['id']}"] = post["title"]  # same story from two sources in one run
        self.log(f"  KLAAR: {post['title']} ({c['url']})")
        self._set(c, "ready", k.get("reason", ""), published=published, title=title, data=data)

    def _post(self, c, src, page, published, k, w) -> dict:
        regio = ["vlaanderen"]
        if k.get("region") == "belgie-federaal":
            regio.append("belgie-federaal")
        regio += k.get("onderwijsnet", []) + k.get("extra_regio", [])
        return {
            "title": w["title"].strip().strip('"'),
            "date": published,
            "niveau": _uniq(k.get("niveau", [])),
            "vak": _uniq(k.get("vak", [])) or ["vakoverschrijdend"],
            "thema": _uniq(k.get("thema", []))[:3],
            "regio": _uniq(regio),
            "type": k.get("type", "nieuws"),
            "doelgroep": _uniq(k.get("doelgroep", [])),
            # The bron is the URL we actually fetched; the model never supplies URLs.
            "bron": vocab.clean_url(c["raw_url"] if c["raw_url"].startswith("https://")
                                    else page.url),
            "image": page.image or (c["image"] if c["image"].startswith("https://") else ""),
            "body": re.sub(r"\s+", " ", w["body"]).strip(),
            "bronnaam": src["name"],
            "prefix": src.get("prefix", c["source"]),
            "slug": w["slug"],
        }


def publish_candidates(cands: list[dict], run_log) -> str:
    """Commit + push ready candidates in one commit. Expects a fresh repo.sync()."""
    existing = vocab.existing_bron_urls(repo.REPO_DIR)
    files, used = {}, []
    for c in cands:
        if c["url"] in existing:
            db.update_candidate(c["id"], status="duplicate",
                                reason="intussen al op de site (bv. via Cowork)")
            continue
        post = c["data"]["post"]
        path = repo.post_path(post["date"], post["prefix"], post["slug"])
        rel = str(path.relative_to(repo.REPO_DIR))
        while rel in files:  # two posts in this batch with the same slug
            path = path.with_name(path.stem + "-b.md")
            rel = str(path.relative_to(repo.REPO_DIR))
        files[rel] = repo.render_article(post)
        used.append((c, rel))
    if not files:
        return ""
    commit = repo.publish(files, f"Nieuwe artikels: {date.today().isoformat()}", run_log)
    for c, rel in used:
        data = c["data"]
        data.update(post_path=rel, commit=commit)
        db.update_candidate(c["id"], status="published", data=data,
                            reason=f"gepubliceerd in {commit[:7]}")
    return commit


def run(mode: str, run_log) -> dict:
    _check_llm_config()
    repo.sync()
    p = Pipeline(run_log)

    # Items waiting in review that meanwhile appeared on the site (Cowork in parallel).
    for c in db.candidates_by_status("ready", 500):
        if c["url"] in p.existing:
            db.update_candidate(c["id"], status="duplicate",
                                reason="intussen al op de site (bv. via Cowork)")
            p.counts["ready_now_duplicate"] += 1

    pending = db.candidates_by_status("new", MAX_PROCESS, oldest_first=True)
    run_log(f"{len(pending)} kandidaten te verwerken (modus: {mode})")
    for c in pending:
        p.process(c)

    commit, push_error = "", ""
    ready = db.candidates_by_status("ready", 500, oldest_first=True)
    published = 0
    if mode == "auto" and ready:
        batch = ready[:MAX_POSTS]
        try:
            commit = publish_candidates(batch, run_log)
            published = len(batch) if commit else 0
        except repo.PushError as exc:
            push_error = str(exc)
            run_log(push_error)

    c = p.counts
    parts = [f"{len(pending)} kandidaten verwerkt"]
    if mode == "auto":
        parts.append(f"{published} artikel(s) gepubliceerd")
        if len(ready) > published:
            parts.append(f"{len(ready) - published} wachten")
    else:
        parts.append(f"{c['ready']} nieuw klaar voor review ({len(ready)} in de wachtrij)")
    for key, label in (("irrelevant", "niet relevant"), ("elsewhere", "niet Vlaams"),
                       ("duplicate", "al op de site"), ("gated", "gefilterd"),
                       ("old", "te oud"), ("invalid", "ongeldig"),
                       ("fetch_error", "niet op te halen"), ("deferred", "uitgesteld")):
        if c[key]:
            parts.append(f"{c[key]} {label}")
    summary = "; ".join(parts) + "."
    status = "ok"
    if push_error or p.llm_errors or c["invalid"]:
        status = "warning"
    if push_error:
        summary += f" Push mislukt: {push_error[:200]}"
    return {"status": status, "summary": summary, "commit": commit,
            "items": published if mode == "auto" else c["ready"], "cost": p.cost}
