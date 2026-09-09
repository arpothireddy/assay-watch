# assay-watch

A data-collection spine for luxury-watch listings. It takes a **daily,
append-only snapshot** of public listings from permitted, structured sources so
that price and time-on-market can be analysed over time.

This repository is **Phase 0**: the snapshot pipeline only. There is no web UI,
no public API, no price modelling, and no user accounts here — those are later
phases. The one thing that matters in this phase is a correct, reliable snapshot
job running end to end.

## What it does

- Reads a curated list of watch references from `config/references.yaml`.
- For each enabled reference, asks each enabled **source adapter** for current
  listings.
- Writes every listing as an immutable row in `listing_snapshots`, plus one row
  per adapter run in `crawl_runs`.
- Never updates or deletes a snapshot — history is the asset, and everything
  downstream is re-derivable from these raw rows.

Sources are pluggable and fail independently: one dead source degrades coverage,
it never stops the crawl.

## Crawling policy

The crawler only touches sources whose terms permit automated access. It honours
`robots.txt` per source, identifies itself with a descriptive User-Agent and a
contact URL, rate-limits conservatively (default no faster than 1 request every
3 seconds), and backs off on errors. It never uses residential proxies or
CAPTCHA-solving services. Per-source terms and the chosen access approach are
recorded in [`docs/SOURCES.md`](docs/SOURCES.md).

## Stack

Python 3.12 · PostgreSQL 16 · `httpx` + `selectolax` · SQLAlchemy + Alembic ·
`pydantic-settings` · `structlog` · Prometheus (textfile collector). Managed with
[`uv`](https://docs.astral.sh/uv/). Runs entirely on free-tier / self-hosted
infrastructure.

## Quick start (local)

```bash
cp .env.example .env          # fill in local values
docker compose up -d db       # Postgres on 127.0.0.1 only
uv sync                       # create the venv and install deps
uv run alembic upgrade head   # create the two tables
uv run assay crawl --dry-run  # fetch + parse, write nothing
uv run assay crawl            # a real snapshot run
uv run assay backfill-check   # per-reference days-of-history report
```

Or run the whole stack in containers with `docker compose up`.

## Layout

| Path | What lives there |
|---|---|
| `config/` | References and source registry — pure data, no code |
| `src/assay_watch/` | The importable package (adapters, crawl runner, db, cli) |
| `migrations/` | Alembic schema history |
| `scripts/` | Ops shell: backup and restore-check |
| `tests/` | Test suite |
| `docs/` | `SOURCES.md` — per-source terms and access approach; `DEPLOY.md` — CI/CD setup |

## Deploy

CI (`ci.yml`) runs ruff, mypy --strict, pytest, gitleaks, pip-audit, and a
Docker build sanity check on every push and PR.

Deploy is Cloud Build → Artifact Registry → Cloud Run Jobs, on push to `main`,
with Cloud Scheduler firing the daily crawl. Postgres runs on Neon (free
tier) rather than Cloud SQL, which has no free tier. Expected cost: $0/month.
See [`docs/DEPLOY.md`](docs/DEPLOY.md) for the one-time setup and a testing
checklist; the pipeline itself is [`cloudbuild.yaml`](cloudbuild.yaml).

## License

TBD.
