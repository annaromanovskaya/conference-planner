# Conference Scout Progress

Last updated: 2026-09-05

## Current stage

Foundation/persistence and source-backed discovery are complete. Monitoring/reminders are next.

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

## Remaining

- Implement monitoring, deadline history, reminders, and scheduling.
- Implement Telegram UX.
- Add Docker deployment, documentation, and final verification.

## Verification

- `pytest -q`: 12 passed.
- `ruff check src tests`: passed after formatting.
- Focused search/web/deduplication suite: 13 passed.
- Expanded search/web/AI/deduplication suite: 20 passed.
- Full unit and discovery integration suite: 37 passed; Ruff passed.
- Async SQLite tests require execution outside the restricted development sandbox because `aiosqlite` uses worker threads; this does not affect the container runtime.

## Latest major commit

`54b7643 feat: establish application foundation and persistence` (discovery commit pending).

## Uncommitted work

- Complete integrations/discovery stage, ready for its major commit.
