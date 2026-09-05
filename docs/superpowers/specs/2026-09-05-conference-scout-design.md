# Conference Scout Design

## Purpose and scope

Conference Scout is a personal, self-hosted Telegram bot that discovers scientific conferences, preserves the provenance of important dates, monitors tracked events, and notifies its owner about deadline changes and approaching deadlines.

Each deployment serves exactly one owner identified by `OWNER_TELEGRAM_ID`. Other people use the project by deploying their own Docker Compose instance. The MVP has no registration, invitations, roles, shared workspaces, web UI, or multi-user administration.

The implementation covers the complete MVP defined in `TASK.md`: topic normalization, trusted sources, manual and scheduled discovery, SearXNG search, OpenRouter extraction and relevance scoring, conference tracking, deadline provenance and history, reminders, Telegram management flows, Docker deployment, tests, and operator documentation.

## Architectural approach

The application is a modular async Python monolith. One `conference-scout` process runs aiogram long polling and APScheduler in the same asyncio event loop. SearXNG is a separate internal-only container. SQLite is the durable store on a Docker volume mounted at `/data`.

Application modules have narrow responsibilities:

- `bot` owns Telegram authorization, FSM conversations, callbacks, keyboards, and presentation.
- `topics` and `sources` own the owner's search preferences.
- `conferences` owns query generation, discovery orchestration, verification, deduplication, tracking, and monitoring.
- `reminders` calculates due notifications and prevents duplicates.
- `ai`, `search`, and `web` implement external integration boundaries.
- `db` defines SQLAlchemy models, repositories, sessions, and transactional operations.
- `scheduler` wires configured schedules to the same application services used by manual commands.

Business logic depends on protocols rather than concrete integrations. `LLMProvider` abstracts OpenRouter, `SearchProvider` abstracts SearXNG, and a page-fetching boundary allows a future browser implementation without changing discovery services.

## Single-owner model

At startup the application resolves or creates one internal `User` row from `OWNER_TELEGRAM_ID` and builds an immutable `OwnerContext`. Every Telegram update passes through owner-only authorization before reaching handlers. Unauthorized callers receive a short private-instance response and no configuration details.

The database retains explicit `user_id` relationships for topics, subscriptions, reminders, and feedback. They provide referential integrity and avoid hidden global IDs, but are not exposed as multi-user behavior. There is no `/register` command or runtime owner switching. Changing the owner is an operator action documented in the README.

Trusted sources are global within the personal deployment.

## Deterministic discovery pipeline

Discovery follows a fixed pipeline:

1. Load enabled topics and validated normalized keywords.
2. Generate deterministic queries for configured event types, current year, and next year.
3. Add `site:` queries for enabled trusted domains and enqueue enabled trusted pages.
4. Search through `SearchProvider`, isolating failures per query.
5. Normalize and pre-deduplicate candidate URLs.
6. Validate each URL and retrieve bounded page content safely.
7. Convert HTML to bounded plain text and label it as untrusted data.
8. Ask the LLM to extract only facts visible in that text.
9. Validate the structured result with Pydantic v2.
10. Verify official URLs and date provenance using retrieved sources.
11. Deduplicate conferences using deterministic evidence.
12. Score topic relevance independently of source confidence.
13. Persist accepted candidates and send a Telegram summary.

The LLM never generates a conference without a retrieved URL. Missing facts remain `null`. Invalid model output receives a bounded retry and is discarded after the retry limit without modifying existing records.

## External integrations

`OpenRouterLLMProvider` uses the configured API key and model and requests structured JSON. Separate prompts cover topic normalization, conference extraction, and relevance ranking. Prompts state that page text is untrusted data, instructions embedded in it must be ignored, external knowledge must not be used, and absent values must be returned as null.

`SearXNGSearchProvider` calls the internal JSON search endpoint and converts results to a provider-neutral `SearchResult`. Timeouts, HTTP errors, temporary backend failures, empty responses, and malformed payloads are reported as typed integration failures. Discovery catches those failures at query boundaries.

The HTTP page fetcher accepts only HTTP and HTTPS, applies timeouts, a fixed user agent, response-size and redirect limits, and an HTML-compatible content-type policy. It validates the initial URL and every redirect target. DNS results must all be public addresses; loopback, private, link-local, reserved, multicast, unspecified, metadata, and Docker-internal destinations are rejected. The configured SearXNG endpoint is a separate trusted integration and does not use the public page fetcher.

JavaScript-only pages produce a recoverable extraction failure. A headless browser is outside the MVP.

## Persistence model

SQLAlchemy 2 async models cover:

- users;
- topics with JSON keywords;
- trusted sources;
- conferences;
- conference-topic relevance links;
- deadlines;
- subscriptions;
- reminders;
- deadline history;
- conference feedback.

Conference status is `DISCOVERED`, `TRACKING`, or `ARCHIVED`. Deadline type is `ABSTRACT_SUBMISSION`, `REGISTRATION`, `EARLY_REGISTRATION`, or `OTHER`. Trusted source type is `DOMAIN` or `PAGE`.

Every deadline records its value, source URL, verification state, and last check time. `verified=true` requires an official conference or organizer source. A trusted source affects source confidence but does not automatically make a date official.

Changing a deadline is one database transaction that updates the current deadline and inserts a history row with old value, new value, source URL, and detection time. Notification occurs only after commit. A unique database constraint on `(user_id, deadline_id, days_before)` prevents duplicate reminders; a reminder record is created only after successful Telegram delivery.

The application creates the initial schema at startup for the MVP. Model metadata and session boundaries remain compatible with adding Alembic when post-release migrations are needed.

## Deduplication and relevance

Deduplication uses increasingly permissive deterministic evidence:

1. canonical official URL;
2. normalized name or acronym combined with compatible event dates;
3. conservative RapidFuzz title similarity combined with corroborating dates or domain evidence.

Ambiguous candidates remain separate. Similar subject matter alone never merges events. Semantic LLM deduplication is excluded from the MVP.

Relevance is a `0.0` to `1.0` topic-match score validated by Pydantic and stored per conference-topic pair. It does not represent prestige or source trust. Results below `MIN_RELEVANCE_SCORE` are normally hidden from discovery output but can remain observable in structured logs.

## Telegram behavior

The main menu provides topics, discovery, conferences, upcoming deadlines, sources, and settings/help. Topic creation is an FSM flow: free text, LLM normalization preview, then save, edit, or cancel. Source management supports list, add, enable, disable, and delete for domains and pages.

Manual discovery acknowledges the request before running and later sends a summary and conference cards. Cards show relevance, event dates, location, deadline provenance, and verification state, with actions to track, inspect, or mark not interesting. Conference lists separate discovered, tracked, and archived entries.

The bot calculates calendar-day differences in Python using the configured application timezone. It never delegates date arithmetic or reminder decisions to the LLM.

## Scheduling and monitoring

APScheduler runs three jobs:

- weekly discovery at configured weekday and hour;
- daily monitoring at the configured hour for tracked conferences only;
- hourly reminder processing.

Jobs use coalescing and a single-instance limit so downtime does not create a burst of overlapping work. Manual and scheduled discovery share one service.

Monitoring reloads the official website and known deadline source URLs. Failures are isolated per conference and source. Confirmed changes create history transactionally and then request an owner notification. Reminder processing uses configurable day offsets and deterministic date arithmetic.

## Errors and logging

Retries are bounded and use backoff only for transient external failures. A failed query, candidate, or monitored conference cannot terminate the surrounding batch. Structured logical events include discovery start/completion, query outcomes, candidate acceptance/rejection, merges, monitoring, deadline changes, reminders, and integration failures.

Tokens, API keys, authorization headers, and complete environment contents are never logged. Invalid existing data is not overwritten by failed extraction.

## Deployment and health

Docker Compose starts `conference-scout` and internal SearXNG. The application image runs as a non-root user with a read-only root filesystem, `/data` persistent volume, `/tmp` tmpfs, all Linux capabilities dropped, and `no-new-privileges`. It receives no Docker socket, host networking, privileged mode, or inbound Telegram port. SearXNG is not published to the host.

The healthcheck verifies that the application process is responsive and SQLite can execute a lightweight query. OpenRouter, Telegram, SearXNG, and general internet availability are not container-health requirements.

Configuration is validated by `pydantic-settings`. Real secrets exist only in environment variables or the ignored `.env`; `.env.example` contains placeholders.

## Testing

Unit tests cover query generation, date validation, URL safety, deterministic deduplication, reminder calculation and duplicate protection, relevance thresholds, transactional deadline changes and history, Pydantic validation of model output, and owner-only Telegram access.

Integration tests use temporary SQLite and fake providers for:

- search result → fetched page → extracted candidate → persisted conference;
- existing deadline → newly extracted date → history row → notification request.

No automated test requires live Telegram, OpenRouter, SearXNG, or internet access. Completion verification runs Ruff formatting/checks, all pytest suites, Docker image build, `docker compose config`, a repository secret scan, `.gitignore` inspection, and a README-to-implementation review.

## Documentation and progress

The README presents Conference Scout as a personal self-hosted service and documents bot creation, OpenRouter, SearXNG, configuration, Docker deployment, trusted sources, discovery, tracking, backup/restore, security, development, tests, limitations, and roadmap.

`PROGRESS.md` records the active stage, completed work, remaining work, verification evidence, and latest major commit. Work is grouped into approximately five substantial commits: foundation/persistence; integrations/discovery; monitoring/reminders/scheduler; Telegram UX; deployment/docs/final verification.

## Explicit non-goals

The MVP does not include a web frontend, mobile app, OAuth, registration, shared instances, billing, embeddings, vector storage, RAG, autonomous agents, a headless browser, Celery, Redis, Kafka, Kubernetes, or a trainable recommendation system.

