# educaition-runner

Zelfstandige Docker-runner voor de geplande taken van [educaition.today](https://www.educaition.today) — het EducAItion-equivalent van de Positron-admin, maar met website-scraping als invoer in plaats van alleen RSS. Het plan en de fasering staan in [PLAN.md](PLAN.md).

De versie staat in `app/version.py` en in de voettekst van de webinterface.

Eén container bevat een scheduler (APScheduler, cron-tijden in Europe/Brussels), de pipelines en een webinterface (standaard op poort 8095 van de host, in te stellen met `RUNNER_PORT` in `.env`).

## Architectuur

```
docker-compose (1 container, restart: unless-stopped, volume ./data)
└── FastAPI app (poort 8080 in de container → RUNNER_PORT op de host, standaard 8095)
    ├── APScheduler ── cron per taak (tasks/tasks.yml)
    ├── bronnen-verzamelen (elke 2 uur, geen LLM)
    │     tasks/sources.yml → rss | sitemap | html → trefwoordfilter → tabel candidates
    │     (genormaliseerde URL = sleutel: elke URL wordt maar één keer bekeken)
    ├── artikelen-vlaanderen (dagelijks 05:08)
    │     ontdubbelen tegen _posts → pagina ophalen (trafilatura) → datumvenster
    │     → LLM "classify" (relevant? tags uit _data/tags.yml, zelfde nieuws als een post?)
    │     → LLM "write" (titel + 2-4 zinnen) → post renderen + valideren in code
    │     → shadow: wachtrij /review   ·   auto: commit + push (max. 6 per run)
    ├── agent (v1)      Claude Agent SDK op tasks/*.md — voor de taken zonder pipeline, mode: auto
    └── web UI          dashboard · review · kandidaten · bronnen · vergelijking · instellingen
```

Het LLM schrijft nooit bestanden en voert geen git-commando's uit: het geeft enkel JSON terug. De code bouwt de front matter, controleert tags, links en de excerpt-regel, draait `scripts/genereer-tagpaginas.py` en commit/pusht.

## Deployment (Docker-host, naast Positron)

Vereisten: Docker, een OpenAI API-key (platform.openai.com) en een GitHub-token met schrijftoegang tot `rvanbruggen/educaition` (fine-grained PAT, alleen dat repo, permissie "Contents: read and write").

```bash
cd ~/educaition-runner
cp .env.example .env && nano .env           # OPENAI_API_KEY en GITHUB_TOKEN
docker compose up -d --build
```

Daarna open je `http://<host>:8095` (of de poort die je in `RUNNER_PORT` zette). Updaten na een `git push` vanaf je Mac: `~/educaition-runner/deploy.sh` (pull + rebuild, zoals bij Positron).

Eerste test: klik **Run nu** bij "Bronnen verzamelen" en daarna bij "Artikelscan Vlaanderen", en bekijk het resultaat in **Review** en **Kandidaten**.

### Bereikbaarheid buitenshuis (optioneel)

De UI is standaard alleen op je thuisnetwerk bereikbaar. Wil je hem ook onderweg zien: installeer [Tailscale](https://tailscale.com) op de host en je telefoon, en surf naar `http://<tailscale-naam>:8095`. Zet in dat geval ook `ADMIN_PASSWORD` in `.env` (gebruiker: `rik`).

## Fase 1: schaduwmodus

De artikelscan staat op `mode: shadow`: hij draait elke ochtend, maar zet niets online. De Cowork-taken staan sinds 8 oktober 2026 uit, dus artikels komen alleen nog online via **Review**.

- **Review**: wat de runner zou publiceren. Publiceer of wijs af.
- **Vergelijking**: elk artikel dat op de site kwam (tot 8 oktober door Cowork, daarna via Review of handmatig), en wat de runner met dezelfde URL deed ("niet gezien" = bron ontbreekt of de feed was al doorgeschoven).
- **Bronnen**: per bron of hij werkt en wat hij oplevert.

Vertrouw je de selectie, zet dan in `tasks/tasks.yml` `mode: auto` bij `artikelen-vlaanderen`: dan publiceert de scan zelf (max. 6 per run).

## Modellen en kosten

Elke stap (`classify`, `write`, `digest`) heeft een eigen model in de vorm `provider:model`, standaard `openai:gpt-6-luna`. Wijzigen via **Instellingen** (meteen actief, wordt bewaard in de databank) of via `LLM_*` in `.env` als standaardwaarde. Ondersteunde providers: `openai` en `anthropic` (dan is `ANTHROPIC_API_KEY` nodig). De kosten per run staan op het dashboard; de prijstabel staat in `app/llm.py`.

## Beheer

- Bron toevoegen of aanpassen: `tasks/sources.yml`, daarna `docker compose restart`.
- Tijdstip aanpassen: op het dashboard, per taak, via **Schema aanpassen** (om de zoveel uur / elke dag / op weekdagen, plus tijdstip; onder *Geavanceerd* kan ook een 5-velden cron). Meteen actief, bewaard in de databank; **Terug naar standaard** zet de waarde uit `tasks/tasks.yml` terug. Modus (`off`/`shadow`/`auto`) of de standaardtijd aanpassen: `tasks/tasks.yml`, daarna `docker compose restart`.
- Logs: `docker compose logs -f` en de transcripts in `./data/logs/` (ook via elke rundetailpagina).
- Databank (runs, kandidaten, bronstatus, instellingen): `./data/runner.db` (SQLite).

## Veiligheid

- `.env` bevat de API-keys en het GitHub-token en staat in .gitignore — nooit committen.
- Het GitHub-token heeft alleen toegang tot het educaition-repo; foutmeldingen van git worden ontdaan van het token voor ze gelogd worden.
- De v1-agenttaken draaien met `bypassPermissions` binnen de container.
