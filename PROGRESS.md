# Conference Scout Progress

Last updated: 2026-09-05

## Current stage

Implementation, application composition, deployment files, and documentation are complete.
Docker runtime verification is blocked because Docker is unavailable in the current WSL environment.

## Completed

- Read and analyzed `TASK.md`.
- Chose a modular async monolith with a separate internal SearXNG container.
- Confirmed a strictly personal, single-owner deployment model.
- Agreed on deterministic discovery, deadline provenance, monitoring, reminders, security, testing, and deployment boundaries.
- Saved the approved design in `docs/superpowers/specs/2026-09-05-conference-scout-design.md`.
- Saved the executable plan in `docs/superpowers/plans/2026-09-05-conference-scout-implementation.md`.
- Added Python 3.12 packaging, validated environment configuration, and secret-safe examples.
- Added strict Pydantic schemas and enums for topics, conferences, deadlines, relevance, and search results.
- Added the complete SQLAlchemy SQLite entity model, async session setup, owner bootstrap, and core repositories.
- Verified database-level duplicate reminder protection and transactional deadline history behavior.
- Added deterministic query generation for event types, years, and trusted domains.
- Added URL normalization and SSRF rejection for unsafe schemes, credentials, and non-public IP addresses.
- Added bounded HTML-to-text parsing that removes active and navigation content.
- Added conservative deterministic conference identity matching.
- Added the provider-neutral search protocol and SearXNG JSON adapter with malformed-response handling.
- Added a bounded HTML fetcher with content-type checks and redirect revalidation.
- Added the `LLMProvider` protocol, OpenRouter adapter, prompt-injection boundary, structured extraction validation, and bounded retries.
- Added topic normalization and relevance scoring with validated structured responses.
- Added discovery orchestration with per-query/per-candidate isolation, trusted domain/page use,
  provenance verification, relevance thresholding, and SQLite persistence.
- Added deterministic reminder offset calculation and a reminder processing service that records
  only successful deliveries and suppresses repeats.
- Added stable APScheduler weekly/daily/hourly jobs with coalescing and single-instance limits.
- Added SQLite-backed reminder candidate selection for tracked subscriptions and persisted duplicate checks.
- Added daily monitoring of official/deadline URLs with transactional current-date updates, history rows,
  source verification, and post-commit change DTOs for Telegram notification.
- Added owner-only Telegram middleware that rejects other IDs without exposing configuration.
- Added Russian main menu, topic normalization preview/confirmation, trusted source input and controls,
  manual discovery, conference grouping/tracking/archive actions, and upcoming deadline output.
- Added repository-backed topic, trusted-source, and conference tracking operations.
- Added the composition root, per-update repository sessions, scheduled job wiring, graceful shutdown,
  and SQLite-only healthcheck.
- Added a non-root multi-stage Dockerfile, hardened Compose services, internal SearXNG JSON config,
  `.dockerignore`, GitHub-ready README, and MIT license.

## Remaining

- Run `docker compose config` and `docker build -t conference-scout:test .` on a host with Docker.

## Verification

- `pytest -q`: 12 passed.
- `ruff check src tests`: passed after formatting.
- Focused search/web/deduplication suite: 13 passed.
- Expanded search/web/AI/deduplication suite: 20 passed.
- Full unit and discovery integration suite: 37 passed; Ruff passed.
- Focused reminder/scheduler suite: 8 passed; Ruff passed.
- Full suite after monitoring/reminders: 47 passed; Ruff format/check passed.
- Full suite after Telegram UX: 56 passed; Ruff passed.
- Final local suite: 62 passed; Ruff format/check passed.
- `uv build`: source distribution and wheel built successfully.
- Application/healthcheck imports: passed.
- Secret scan: no token/API-key patterns found; `.env`, virtualenvs, and databases are ignored.
- Docker verification attempted but could not start: `docker` is not installed/enabled in this WSL distro.
- Async SQLite tests require execution outside the restricted development sandbox because `aiosqlite` uses worker threads; this does not affect the container runtime.

## Latest major commit

`d7d8f85 feat: add personal Telegram bot interface` (deployment commit pending).

## Uncommitted work

- Final deployment/documentation stage, ready for its major commit.
