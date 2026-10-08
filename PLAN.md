# educaition-runner v2: plan

Status: approved 2026-10-08. Phase 0 done; phase 1 next.

## Decisions (2026-10-08)
- **Host:** same Docker host as `positron-admin` (`~/educaition-runner` beside `~/positron-today`), deployed with `deploy.sh`.
- **Publishing:** auto-publish when validation passes. FAQ/discussie proposals and new books still go to the review queue (see §3).
- **Models:** default `gpt-6-luna` for every step, configurable per step (classify / write / digest) and per provider (OpenAI, Anthropic), so Sol or Claude can be swapped in without code changes.
- **No paid search API for now.** The `search` strategy in §3 is dropped from phase 1. Long-tail recall comes from a wider source list plus the per-source yield view instead. Revisit if shadow mode shows we miss too much.

## 1. What runs in Cowork today

There are seven `educaition-*` tasks in `~/Documents/Claude/Scheduled/`. Six are active. The run history in `educaition/claude-dashboard/runs.json` covers Aug 23 to Oct 7 (63 runs, 2 warnings), and the git history matches it.

| Task | When | What it does | Fit for a scraping pipeline |
|---|---|---|---|
| `artikelen-vlaanderen` | daily 05:08 | WebSearch for Flemish news, 0-6 posts, og:image, push | **Very good.** 80% of the Flemish posts come from about 10 domains (vrt.be 12, veto.be 8, schoolit.be 5, vaia.be 4, g-o.be 3, knack.be 3, bruzz.be 3, vub.be 4, vlaamsparlement.be 3). None come from De Standaard, De Morgen or HLN. |
| `digest-vlaanderen` | Sun 18:07 | 3-5 items, built mostly from the week's posts | **Very good.** Its input is already in the repo. |
| `digest-internationaal` | Sun 18:30 | 3-5 international items plus "Wat betekent dit voor Vlaanderen?" | **Good**, given a list of international sources (most of them have RSS). This task needs the most writing quality. |
| `academisch-wekelijks` | Sun | OpenAlex and arXiv, 1-3 papers | **Excellent.** These are already APIs, so the input is deterministic. |
| `media-wekelijks` | Sun | Podcasts/vodcasts with Spotify/YouTube embed IDs, new books, Goodreads refresh | **Medium.** Podcast and YouTube RSS work well. Spotify episode IDs need the Spotify API. Goodreads scraping is fragile. |
| `faq-discussie-onderhoud` | Sun 19:20 | Conservative edits to `faq.html` and `_data/discussies.yml` | **Weak for autopilot.** This is judgement work, so it should go through review. |
| `educaition-weekly-digests` | (Aug 19) | Old combined digest, never pushed | **Obsolete.** It still uses the github.io URL and the old `site.baseurl` link rule. Delete it. |

What goes wrong in Cowork today:
- On 09-30 and 10-01, posts were written but never pushed because "de repo kon niet in de shell-omgeving gemount worden". Rik caught this on 10-04.
- The prompts carry workarounds for the sandbox: stale `.git/*.lock` files, a relative credential helper, folder access requests, and republishing the dashboard artifact.
- The daily scan does a fresh web search every day and keeps rediscovering the same duplicates ("alle kandidaten waren duplicaten" appears in about half of the runs). Nothing remembers which URLs were already seen or rejected.

## 2. State of this repo (educaition-runner v1, 2026-08-22)

v1 is a FastAPI app with APScheduler, the Claude Agent SDK and a SQLite run log. The code is clean but was never deployed: there is no `data/` folder and the folder is not a git repo. It has these problems:
- `GITHUB_REPO=rikvanbruggen/educaition` in `.env.example` and the README. The real remote is **`rvanbruggen/educaition`**.
- It includes only 4 of the 6 tasks. Academisch and media are missing.
- The prompts are the Aug 22 versions. They are missing the Sep 9 changes: the og:image field, the EXCERPT RULE, and step 4b (`scripts/genereer-tagpaginas.py` plus `niveau/` pages). Pushes would fail the check-links workflow whenever a new niveau appears.
- It uses the same design as Cowork: an LLM agent with web search for up to 150 turns. That costs more and is just as non-deterministic. The runner only changes where the agent runs.

**What to keep:** the container layout, the repo sync (`sync_repo`), `RUN_LOCK`, the SQLite run history, the dashboard and run pages, `tasks.yml` cron config, and the `report.json` status model (ok/warning/error).
**What to replace:** the agent execution itself. It becomes a pipeline like Positron's.

## 3. Target architecture: Positron-style, with scraping instead of RSS

```
Docker (1 container "educaition-runner", same host as positron-admin, port 8080)
└── FastAPI + APScheduler + SQLite (/data)
    │
    ├── COLLECT   (no LLM, every 4h)
    │   sources.yml: per source a strategy
    │     rss | sitemap (news-sitemap/lastmod) | html (listing URL + CSS selector)
    │     | api (OpenAlex, arXiv, podcast RSS, YouTube channel RSS, Spotify)
    │     | search (fallback discovery: daily search-API queries from the current prompt)
    │   → fetch article → trafilatura (text) + htmldate/JSON-LD (date) + og:image
    │   → candidates table (normalised URL = unique key, so a URL is seen only once)
    │
    ├── FILTER    (no LLM) keyword gate AI×onderwijs, date window, dedupe against bron: in _posts
    │
    ├── CLASSIFY  (gpt-6-luna, structured output)
    │   relevant? region? type? niveau/thema/doelgroep as an ENUM taken from tags.yml
    │   rejections are kept with a reason ("what was skipped", as in Positron)
    │
    ├── WRITE     article summary: Luna · digests/duiding/academisch: Luna or Sol (A/B test)
    │   the LLM returns JSON fields only; the code renders front matter and Markdown
    │
    ├── VALIDATE  in code: YAML, tags, https bron/image, post_url targets, excerpt rule
    │             (first sentence has no "o.a."), then run genereer-tagpaginas.py
    │
    └── PUBLISH   git commit + push from /data/repo (token in the remote, never in logs)
                  mode per task: auto | review (queue in the web UI, one-click publish)
```

Why Python and not a fork of the Positron admin (Next.js): v1 is already Python, the best scraping libraries are Python (trafilatura, htmldate, feedparser), and the site's own scripts are Python. The ideas carry over from Positron, not the code: a provider abstraction like `lib/llm.ts`, the reject log, `run-lock`, and settings in the database.

**The key change from today:** the LLM never decides which files to write or which git commands to run. It only answers small questions with a fixed output schema. Invalid tags or invalid YAML become impossible by construction instead of being "validated afterwards", and the git and lock problems go away.

### Per task in v2

1. **Articles (daily).** Collection runs every 4h and publishes once a day at 05:08. Start with sources from the domain list in §1, plus Klasse, KlasCement, onderwijs.vlaanderen.be, Vlor, KOV, OVSG, Mediawijs, imec and the universities and colleges. Keep the "search" strategy as a safety net for the long tail (BRUZZ, Apache, Trends and accountancyvandaag were long-tail finds), using the existing query list from the prompt.
2. **Digest Vlaanderen (Sunday).** Input is the week's posts plus candidates that were relevant but not published. One write call.
3. **Digest internationaal (Sunday).** International candidates are collected all week (EC, OECD, UNESCO, Kennisnet, Npuls, DfE, TES, Schools Week, EdSurge, EdWeek, K-12 Dive, and the education blogs of OpenAI, Google, Microsoft and Anthropic). Luna ranks for relevance to Flanders. The stronger model writes the duiding.
4. **Academisch (Sunday).** OpenAlex and arXiv through their APIs. Luna scores (RCT, meta-analysis or Flemish → priority) and summarises using the EXCERPT RULE.
5. **Media (Sunday).** Podcast RSS for the four curated shows, plus Apple/Podcast Index lookups for new shows and YouTube channel RSS. The Spotify Web API (client credentials, free) maps episodes to `spotify_episode` IDs, so IDs are never guessed. Goodreads refresh runs every 21 days as a separate job; if parsing fails, the post stays unchanged. New books: Open Library / Google Books search, review mode only.
6. **FAQ & Ter discussie (Sunday).** The LLM proposes structured patches (a standpunt for `discussies.yml`, or a change to one specific `<details>` block). These always land in the review queue and are never pushed automatically. Optional: open them as a GitHub PR so the check-links workflow runs first.

### Costs (estimate, check against OpenAI's pricing page)
gpt-6-luna is $0.10 in / $0.50 out per 1M tokens (launched 2026-09-22). Classifying about 50 candidates a day at about 3k tokens each comes to about $0.50/month. All writing work together is a few dollars a month even with Sol ($2/$10). The real cost line is a search API for fallback discovery, if one is used. Either way this is far below a Sonnet agent with 150 turns of web search.

## 4. Phasing

| Phase | Content | Done when |
|---|---|---|
| 0 | Delete `educaition-weekly-digests` in Cowork. `git init` this repo and push it to GitHub (for a `deploy.sh` like Positron's). Fix `GITHUB_REPO`. | — |
| 1 | Collector + `sources.yml` + candidates DB + article pipeline in **shadow mode** (review queue, no push) while Cowork keeps running | After 2 weeks: v2 found ≥ the Cowork articles (compare bron URLs) |
| 2 | Article task on `auto`, **turn off the Cowork task**. Academisch + digest Vlaanderen. | 2 Sundays in a row without manual fixes |
| 3 | Digest internationaal (A/B: Luna vs Sol) + media/Goodreads | Cowork tasks off |
| 4 | FAQ/discussie as proposals in the review queue. Retire `claude-dashboard/` and the Cowork artifact. | All Cowork tasks off |

Ongoing: a "yield per source" view in the dashboard (candidates → relevant → published), so dead sources can be removed and gaps become visible. Each week, compare against one search-API run to measure recall.

## 5. Risks
- **Recall drops** compared to a free agent with web search. Mitigation: the search fallback plus the per-source yield view, and phase 1 runs in shadow mode for exactly this reason.
- **Cookie and paywalls** (DPG, Mediahuis). When a page is blocked, fall back to the RSS/sitemap title and description, or skip the item. These sources produce almost no posts today anyway.
- **Site HTML changes** break `html` sources. Each source tracks its own health, and the dashboard shows a warning after N runs with 0 items.
- **Writing quality of Luna** in Belgian Dutch. A/B test against Sol on the digests. The provider is a setting, so Claude can also be plugged in for the writing step only.
