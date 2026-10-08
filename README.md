# educaition-runner

Zelfstandige Docker-runner voor de geplande Claude-taken van [educaition.today](https://www.educaition.today) — het EducAItion-equivalent van de Positron-admin, maar gebouwd rond agent-runs (websearch + schrijven) in plaats van RSS-feeds.

Eén container bevat drie dingen: een scheduler (APScheduler, cron-tijden in Europe/Brussels), een agent-uitvoerder (Claude Agent SDK, headless met API-key) en een webinterface op poort 8080 (status, run-historiek, transcripts, "Run nu", pauzeren, kosten per maand).

## Architectuur

```
docker-compose (1 container, restart: unless-stopped)
└── FastAPI app (poort 8080)
    ├── APScheduler ── cron per taak (tasks/tasks.yml)
    ├── runner.py ──── per run:
    │     1. git clone/reset van rvanbruggen/educaition naar /data/repo
    │     2. Claude Agent SDK draait de taakprompt (tasks/*.md) in die checkout
    │     3. agent schrijft /data/report.json; runner slaat resultaat + kosten op in SQLite
    └── web UI ─────── dashboard, rundetails, transcripts (volume ./data)
```

De vier taken zijn 1-op-1 overgenomen van de Cowork scheduled tasks (zelfde bronnen, regels, validatie en tijdstippen); alleen de omgevingsstappen verschillen (repo staat al klaar, rapport via JSON-bestand i.p.v. artifact).

## Deployment op de MacBook Air

Vereisten: Docker (zoals voor Positron), een Anthropic API-key (console.anthropic.com) en een GitHub-token met schrijftoegang tot `rvanbruggen/educaition` (fine-grained PAT, alleen dat repo, permissie "Contents: read and write").

```bash
# op de Air
git clone <deze repo> ~/educaition-runner   # of kopieer de map via scp/AirDrop
cd ~/educaition-runner
cp .env.example .env && nano .env           # vul ANTHROPIC_API_KEY en GITHUB_TOKEN in
docker compose up -d --build
open http://localhost:8080                   # of vanaf je telefoon: http://<air-naam>.local:8080
```

Eerste test: klik "Run nu" bij één taak (bv. de artikelscan) en volg het transcript via de rundetailpagina. Controleer daarna of de commit op GitHub staat en de site gebouwd wordt.

Zorg dat de Air niet slaapt: System Settings → schakel automatische sluimerstand uit (of `sudo pmset -a sleep 0 disablesleep 1`).

### Bereikbaarheid buitenshuis (optioneel)

De UI is standaard alleen op je thuisnetwerk bereikbaar. Wil je hem ook onderweg zien: installeer [Tailscale](https://tailscale.com) op de Air en je telefoon (gratis voor persoonlijk gebruik), en surf naar `http://<tailscale-naam>:8080`. Zet in dat geval ook `ADMIN_PASSWORD` in `.env` (gebruiker: `rik`).

## Cutover-plan (van Cowork naar de runner)

1. Draai de runner een paar dagen parallel: laat de dagelijkse artikelscan hier draaien maar **pauzeer hem eerst in Cowork**, anders scant alles dubbel. De veiligheidsgordel: beide varianten dedupliceren op bron-URL, dus een overlap-dag is geen ramp.
2. Vergelijk een week lang de output (kwaliteit, kosten in het dashboard).
3. Tevreden? Schakel in de Claude-app de vier geplande taken uit (educaition-artikelen-vlaanderen, educaition-digest-vlaanderen, educaition-digest-internationaal, educaition-faq-discussie-onderhoud). Het artifact-dashboard mag blijven bestaan maar wordt niet meer bijgewerkt; de web-UI van de runner vervangt het.
4. De map `claude-dashboard/` in het site-repo mag daarna weg (staat in .gitignore, dus alleen lokaal).

## Kosten

Elke run betaalt API-verbruik; het dashboard toont kosten per run en per maand. Model instelbaar via `CLAUDE_MODEL` in `.env` (standaard claude-sonnet-5; goedkoper kan met haiku, maar de schrijfkwaliteit van de digests gaat er dan op achteruit). `max_turns` per taak in `tasks/tasks.yml` is de kostenrem. Verwacht ruwweg: dagelijkse scan ~30 runs/maand plus 3 zondagsruns/week — hou de eerste maand het dashboard in de gaten en stel bij.

## Beheer

- Prompt aanpassen: bewerk `tasks/*.md`, daarna `docker compose restart`.
- Tijdstip aanpassen: `tasks/tasks.yml`, daarna `docker compose restart`.
- Logs: `docker compose logs -f` (app) en de transcripts in `./data/logs/`.
- Databank met run-historiek: `./data/runner.db` (SQLite).
- Updaten na wijzigingen: `docker compose up -d --build`.

## Veiligheid

- `.env` bevat de API-key en het GitHub-token en staat in .gitignore — nooit committen.
- Het GitHub-token heeft alleen toegang tot het educaition-repo.
- De agent draait met `bypassPermissions`, maar binnen een container die niets anders bevat dan de repo-checkout; de prompts verbieden expliciet het printen van de remote-URL (bevat het token).
