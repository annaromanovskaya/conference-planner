# Conference Scout

Conference Scout is a personal, self-hosted Telegram bot that discovers scientific events from real web sources, tracks their dates, and warns its owner when a deadline approaches or changes. One deployment serves one Telegram owner; every other user deploys an independent instance.

## Features

- LLM-assisted topic normalization and relevance scoring through OpenRouter.
- Deterministic current/next-year queries through a private SearXNG container.
- Trusted domains and catalog pages.
- Source-backed extraction, deadline provenance, conservative deduplication, tracking and archives.
- Weekly discovery, daily monitoring, hourly reminders, and deadline-change history.
- Owner-only Telegram access and persistent SQLite storage.

## Architecture

An async Python modular monolith runs aiogram long polling and APScheduler. SQLAlchemy/aiosqlite store state in `/data/conference_scout.db`. Protocol boundaries isolate OpenRouter, SearXNG, and page retrieval. SearXNG is a second internal-only Compose service.

## How it works

Topics become validated keywords. Python generates queries, SearXNG returns real URLs, the SSRF-safe fetcher retrieves bounded HTML, and OpenRouter extracts only facts present in the page. Pydantic validates output before deterministic verification, deduplication, relevance filtering, and persistence. The LLM never acts as a conference or date source.

## Requirements

Docker Engine with Compose v2, a Telegram bot token, the owner's numeric Telegram ID, and an OpenRouter API key/model. Local development requires Python 3.12+ and `uv`.

## Quick start

```bash
git clone YOUR_REPOSITORY_URL conference-scout
cd conference-scout
cp .env.example .env
# edit .env
docker compose up -d
docker compose logs -f conference-scout
```

## Telegram bot creation

Create a bot with Telegram's `@BotFather`, place its token in `TELEGRAM_BOT_TOKEN`, and put your numeric account ID in `OWNER_TELEGRAM_ID`. Messages from every other account are rejected before handlers run.

## OpenRouter configuration

Set `OPENROUTER_API_KEY` and any compatible `OPENROUTER_MODEL`. The code does not pin a model. Model output is retried only a bounded number of times and must pass Pydantic validation.

## SearXNG

Compose starts SearXNG without a host port and enables JSON search. The application reaches it at `http://searxng:8080`; public page SSRF rules do not apply to this explicitly configured internal integration.

## Configuration

All settings are environment variables documented in `.env.example`. `DISCOVERY_DAY` and `DISCOVERY_HOUR` schedule weekly discovery, `MONITORING_HOUR` schedules monitoring, `REMINDER_DAYS` is a comma-separated positive list, `MIN_RELEVANCE_SCORE` is between 0 and 1, and `APP_TIMEZONE` is an IANA timezone. Restart after configuration changes.

## Docker deployment

Run `docker compose up -d`. The application uses long polling and publishes no inbound port. It runs non-root with a read-only root filesystem, dropped capabilities, `no-new-privileges`, writable `/data`, and tmpfs `/tmp`. Both services use `restart: unless-stopped`.

## Trusted sources

Add `DOMAIN example.org` to generate additional `site:` queries, or `PAGE https://example.org/events` to check a catalog directly. Trusted does not mean official: secondary dates remain visibly unverified.

## Topics

Choose “Мои темы”, add natural-language scientific interests, inspect the generated canonical name/description/keywords, then confirm. Nothing is saved before confirmation.

## Discovery

Choose “Найти конференции” for an immediate run. Scheduled discovery covers the current and following calendar year. Errors are isolated per query and candidate.

## Deadline tracking

Track a discovered conference to enable daily monitoring and reminders. Each deadline stores its source URL and verification flag. Date changes update the current record and append history in one transaction; reminders use Python calendar arithmetic and a database uniqueness constraint.

## Backups

Stop the application or use SQLite's online backup command, then copy `/data/conference_scout.db` from the `conference_data` volume. Example:

```bash
docker compose stop conference-scout
docker compose run --rm --entrypoint sh conference-scout -c 'cp /data/conference_scout.db /tmp/conference_scout.db'
docker compose start conference-scout
```

For practical host backups, mount a host directory at `/data` instead of the named volume and copy the database while the service is stopped. Restore by replacing the database before startup.

## Security

Secrets live only in ignored `.env`; never commit it. Telegram access is restricted to `OWNER_TELEGRAM_ID`. Containers are non-privileged, have no Docker socket or host networking, and expose no ports. Search and user URLs are untrusted: only HTTP(S) public destinations are fetched, DNS and redirects are checked, and private/loopback/link-local/metadata addresses are rejected. External HTML is untrusted data, not instructions; prompts explicitly ignore instructions embedded in pages.

## Development

```bash
uv sync --python 3.12 --extra dev
uv run ruff format .
uv run ruff check .
uv run pytest -q
```

SQLite integration tests may require normal thread permissions because aiosqlite uses a worker thread.

## Testing

Tests use fake search/fetch/LLM/Telegram boundaries and temporary SQLite databases; they require no credentials or internet. Unit tests cover configuration, validation, queries, SSRF, parsing, deduplication, relevance, access control, reminders and scheduling. Integration tests cover discovery persistence and deadline history.

## Limitations

JavaScript-only pages are not rendered. Date-only deadlines have no source timezone. Extraction quality depends on the selected model. Initial schema creation is automatic; production schema migrations are a future addition. This is intentionally a personal single-owner service.

## Roadmap

- Alembic migrations and richer Telegram cards/actions.
- Optional browser fetcher for JavaScript-only sites.
- Conference-series modeling and wider configurable search windows.
- Optional additional LLM/search providers behind existing protocols.

## License

MIT. See `LICENSE`.

