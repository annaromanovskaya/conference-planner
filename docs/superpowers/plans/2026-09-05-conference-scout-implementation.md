# Conference Scout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a production-quality personal self-hosted Telegram bot that discovers source-backed scientific conferences and reliably monitors their deadlines.

**Architecture:** An async modular monolith runs aiogram polling and APScheduler, persists through SQLAlchemy to SQLite, and talks to internal SearXNG plus OpenRouter through protocol-based adapters. Deterministic services own discovery, provenance, deduplication, monitoring, and reminder decisions; LLM output is always source-bound and Pydantic-validated.

**Tech Stack:** Python 3.12, aiogram 3, SQLAlchemy 2, aiosqlite, Pydantic 2, pydantic-settings, httpx, BeautifulSoup4, RapidFuzz, APScheduler, pytest, pytest-asyncio, Ruff, Docker Compose, SearXNG.

**Spec:** `docs/superpowers/specs/2026-09-05-conference-scout-design.md`

## Global Constraints

- One Docker Compose deployment serves exactly one owner from `OWNER_TELEGRAM_ID`.
- LLM calls use only `OPENROUTER_API_KEY` and `OPENROUTER_MODEL`; no model name or secret is embedded in code.
- Conferences and dates enter the database only from retrieved HTTP/HTTPS source content.
- Every stored deadline includes source URL, verification state, and last-check time.
- Date arithmetic, change detection, scheduling, IDs, and duplicate reminder protection are deterministic Python/database behavior.
- External failures use bounded retries and are isolated per query, candidate, or monitored conference.
- The page fetcher rejects non-public destinations before requests and at every redirect.
- Application code is async and external services are replaceable by fakes in tests.
- No placeholder implementations, autonomous-agent frameworks, task queues, browser automation, web UI, or multi-user UX.
- Progress is recorded in `PROGRESS.md`; implementation is grouped into five substantial commits.

---

### Task 1: Project foundation, configuration, and domain schemas

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `.env.example`
- Create: `src/conference_scout/__init__.py`, `src/conference_scout/config.py`
- Create: `src/conference_scout/schemas/common.py`, `topics.py`, `conferences.py`, `integrations.py`
- Test: `tests/unit/test_config.py`, `tests/unit/test_schemas.py`

**Interfaces:**
- Produces: `Settings`, `ConferenceStatus`, `DeadlineType`, `SourceType`, `TopicNormalization`, `ConferenceExtraction`, `RelevanceAssessment`, `SearchResult`.
- Consumes: environment variables listed in `.env.example`.

- [ ] **Step 1: Add packaging and test configuration**

Define Python `>=3.12`, runtime/dev dependencies, src layout, strict pytest asyncio mode, and Ruff rules. Ignore `.env`, databases, caches, coverage, and build artifacts.

- [ ] **Step 2: Write failing configuration and schema tests**

```python
def test_settings_parse_reminder_days(valid_environment):
    settings = Settings()
    assert settings.reminder_days == (30, 14, 7, 3, 1)

def test_extraction_rejects_deadline_without_source():
    with pytest.raises(ValidationError):
        ConferenceExtraction.model_validate({
            "name": "Example 2027",
            "source_url": "https://example.org/event",
            "deadlines": [{"type": "ABSTRACT_SUBMISSION", "date": "2027-02-12"}],
        })
```

- [ ] **Step 3: Run tests and verify missing-module failures**

Run: `pytest tests/unit/test_config.py tests/unit/test_schemas.py -q`
Expected: collection fails because `conference_scout.config` and schemas do not exist.

- [ ] **Step 4: Implement typed settings and Pydantic schemas**

```python
class Settings(BaseSettings):
    telegram_bot_token: SecretStr
    owner_telegram_id: int
    openrouter_api_key: SecretStr
    openrouter_model: str
    database_url: str = "sqlite+aiosqlite:////data/conference_scout.db"
    searxng_url: AnyHttpUrl = AnyHttpUrl("http://searxng:8080")
    reminder_days_raw: str = Field("30,14,7,3,1", alias="REMINDER_DAYS")

    @computed_field
    @property
    def reminder_days(self) -> tuple[int, ...]: ...
```

Use strict enums and nested `ExtractedDeadline` values so a non-null date requires a source URL.

- [ ] **Step 5: Run foundation tests and Ruff**

Run: `pytest tests/unit/test_config.py tests/unit/test_schemas.py -q && ruff check src tests`
Expected: all selected tests pass and Ruff reports no violations.

---

### Task 2: Async database, repositories, and owner bootstrap

**Files:**
- Create: `src/conference_scout/db/base.py`, `models.py`, `session.py`
- Create: `src/conference_scout/db/repositories/users.py`, `topics.py`, `sources.py`, `conferences.py`, `reminders.py`
- Create: `src/conference_scout/owner.py`
- Test: `tests/conftest.py`, `tests/unit/db/test_repositories.py`

**Interfaces:**
- Produces: `create_engine_and_sessionmaker(url)`, `initialize_database(engine)`, `OwnerContext`, repositories accepting `AsyncSession`.
- Consumes: enums and validated values from Task 1.

- [ ] **Step 1: Write failing persistence tests**

```python
async def test_owner_is_resolved_by_telegram_id(session):
    owner = await UserRepository(session).get_or_create_by_telegram_id(12345)
    await session.commit()
    assert owner.telegram_id == 12345

async def test_reminder_uniqueness_is_database_enforced(session, deadline):
    repo = ReminderRepository(session)
    await repo.record_sent(1, deadline.id, 14, utc_now())
    with pytest.raises(IntegrityError):
        await repo.record_sent(1, deadline.id, 14, utc_now())
```

- [ ] **Step 2: Verify RED**

Run: `pytest tests/unit/db/test_repositories.py -q`
Expected: missing database modules.

- [ ] **Step 3: Implement models and session lifecycle**

Define all entities in `TASK.md`, timezone-aware timestamps, foreign keys, unique subscription/topic links, and reminder uniqueness. Configure SQLite foreign keys on connect.

- [ ] **Step 4: Implement focused repositories and owner bootstrap**

```python
@dataclass(frozen=True, slots=True)
class OwnerContext:
    user_id: int
    telegram_id: int

async def bootstrap_owner(session: AsyncSession, telegram_id: int) -> OwnerContext: ...
```

Repositories expose explicit query/update methods; no application service emits arbitrary SQL.

- [ ] **Step 5: Verify persistence**

Run: `pytest tests/unit/db -q && ruff check src tests`
Expected: repository tests pass, including actual SQLite constraints.

- [ ] **Step 6: Update progress and create major commit 1**

Record completed scope and verification in `PROGRESS.md`.

Run: `git add . && git commit -m "feat: establish application foundation and persistence"`

---

### Task 3: Query generation, URL safety, page retrieval, and parsing

**Files:**
- Create: `src/conference_scout/search/provider.py`, `query.py`, `searxng.py`
- Create: `src/conference_scout/web/url_safety.py`, `fetcher.py`, `parser.py`
- Test: `tests/unit/search/test_query.py`, `test_searxng.py`
- Test: `tests/unit/web/test_url_safety.py`, `test_fetcher.py`, `test_parser.py`

**Interfaces:**
- Produces: `SearchProvider.search(query) -> list[SearchResult]`, `generate_queries(...)`, `URLSafetyPolicy.validate(url)`, `PageFetcher.fetch(url) -> FetchedPage`, `extract_page_text(html)`.
- Consumes: `SearchResult`, settings timeout/size values, topic keywords, trusted sources.

- [ ] **Step 1: Write failing deterministic query tests**

```python
def test_queries_cover_event_types_years_and_trusted_domain():
    queries = generate_queries(["astroinformatics"], [2026, 2027], ["iau.org"])
    assert "astroinformatics 2026 conference" in queries
    assert "astroinformatics 2027 workshop" in queries
    assert "site:iau.org astroinformatics 2027" in queries
```

- [ ] **Step 2: Write failing URL and fetcher tests**

Test rejection of localhost, IPv4/IPv6 private and link-local addresses, credentials, unsupported schemes, DNS rebinding candidates, redirects to private addresses, oversized bodies, non-HTML content, timeout, and successful bounded HTML retrieval.

- [ ] **Step 3: Verify RED**

Run: `pytest tests/unit/search tests/unit/web -q`
Expected: missing implementation modules.

- [ ] **Step 4: Implement query and search provider boundary**

```python
class SearchProvider(Protocol):
    async def search(self, query: str) -> list[SearchResult]: ...
```

Normalize SearXNG JSON and raise typed retryable/non-retryable errors without leaking backend response bodies into normal logs.

- [ ] **Step 5: Implement SSRF-safe fetch and text extraction**

Resolve hostnames, require every address to be globally routable, pin connection behavior sufficiently to avoid validation/request mismatch, disable automatic redirects, validate each `Location`, stream with a hard byte limit, and strip scripts/styles/navigation from parsed text.

- [ ] **Step 6: Verify web/search tests**

Run: `pytest tests/unit/search tests/unit/web -q && ruff check src tests`
Expected: all selected tests pass.

---

### Task 4: OpenRouter adapter and validated LLM operations

**Files:**
- Create: `src/conference_scout/ai/provider.py`, `openrouter.py`, `prompts.py`, `service.py`
- Test: `tests/unit/ai/test_openrouter.py`, `test_service.py`, `test_prompt_safety.py`

**Interfaces:**
- Produces: `LLMProvider.complete_structured(prompt, schema)`, `AIService.normalize_topic`, `extract_conference`, `rank_relevance`.
- Consumes: Pydantic schemas and bounded untrusted page text.

- [ ] **Step 1: Write failing validation/retry tests**

```python
async def test_extraction_retries_invalid_json_once(fake_provider):
    fake_provider.responses = ["not-json", valid_extraction_json]
    result = await AIService(fake_provider, max_attempts=2).extract_conference(page)
    assert result.name == "Astroinformatics 2027"
    assert fake_provider.calls == 2
```

Also prove that exhausted invalid output raises a typed error and prompt text labels webpage content untrusted.

- [ ] **Step 2: Verify RED**

Run: `pytest tests/unit/ai -q`
Expected: missing AI modules.

- [ ] **Step 3: Implement provider, prompts, and bounded retry**

Use OpenRouter's OpenAI-compatible chat endpoint, configured model, JSON schema response format when supported, timeout, and bounded exponential backoff. Validate every persisted-capable response with the requested Pydantic type.

- [ ] **Step 4: Verify AI tests**

Run: `pytest tests/unit/ai -q && ruff check src tests`
Expected: all selected tests pass with no live network.

---

### Task 5: Deduplication, provenance verification, and discovery orchestration

**Files:**
- Create: `src/conference_scout/conferences/deduplication.py`, `verification.py`, `discovery.py`
- Create: `src/conference_scout/topics/service.py`, `src/conference_scout/sources/service.py`
- Test: `tests/unit/conferences/test_deduplication.py`, `test_verification.py`, `test_relevance.py`
- Test: `tests/integration/test_discovery.py`

**Interfaces:**
- Produces: `DiscoveryService.run(owner) -> DiscoveryReport`, `find_duplicate(candidate, existing)`, `classify_source(...)`.
- Consumes: repositories, `SearchProvider`, page fetcher, parser, AI service, topic/source services.

- [ ] **Step 1: Write failing deduplication and provenance tests**

```python
def test_same_topic_does_not_merge_unrelated_events():
    assert find_duplicate(candidate_a, [candidate_b]) is None

def test_official_domain_verifies_deadline():
    result = verify_deadline(source_url="https://event.org/dates", official_url="https://event.org")
    assert result.verified is True
```

- [ ] **Step 2: Write failing end-to-end discovery integration test**

Compose fake search, page fetcher, and LLM with temporary SQLite. Assert a real candidate URL is fetched before extraction, the conference/deadline/topic relevance are persisted, duplicate results merge, and one bad candidate does not abort the report.

- [ ] **Step 3: Verify RED**

Run: `pytest tests/unit/conferences tests/integration/test_discovery.py -q`
Expected: missing services.

- [ ] **Step 4: Implement deterministic merge and verification**

Canonicalize URLs and names, compare acronym/dates, then apply conservative RapidFuzz matching only with corroborating evidence. Keep source confidence distinct from relevance.

- [ ] **Step 5: Implement discovery service with error isolation**

```python
@dataclass(slots=True)
class DiscoveryReport:
    created_ids: list[int]
    merged_ids: list[int]
    rejected_urls: list[str]
    errors: list[DiscoveryError]
```

Process current/next year, trusted domains and pages, threshold relevance, and persist only validated source-backed candidates.

- [ ] **Step 6: Verify discovery and create major commit 2**

Run: `pytest tests/unit/search tests/unit/web tests/unit/ai tests/unit/conferences tests/integration/test_discovery.py -q && ruff check src tests`

Update `PROGRESS.md`, then run: `git add . && git commit -m "feat: add source-backed conference discovery"`

---

### Task 6: Deadline monitoring and transactional history

**Files:**
- Create: `src/conference_scout/conferences/monitoring.py`
- Extend: `src/conference_scout/db/repositories/conferences.py`
- Test: `tests/unit/conferences/test_monitoring.py`
- Test: `tests/integration/test_deadline_change.py`

**Interfaces:**
- Produces: `MonitoringService.run(owner) -> MonitoringReport`, `DeadlineChange` notification DTO.
- Consumes: tracked conferences, known source URLs, fetcher, AI extraction, repository transaction.

- [ ] **Step 1: Write failing change-detection tests**

Assert unchanged dates create no history, changed dates update current value and history together, failed validation preserves old data, and notifications are returned only after commit.

- [ ] **Step 2: Verify RED**

Run: `pytest tests/unit/conferences/test_monitoring.py tests/integration/test_deadline_change.py -q`
Expected: missing monitoring service.

- [ ] **Step 3: Implement transactional monitoring**

Monitor only subscribed/tracked conferences. Re-fetch official and deadline pages, isolate each conference failure, prefer official evidence, and update each confirmed change through one repository transaction.

- [ ] **Step 4: Verify monitoring**

Run: `pytest tests/unit/conferences/test_monitoring.py tests/integration/test_deadline_change.py -q`
Expected: all selected tests pass.

---

### Task 7: Reminder calculation and scheduler wiring

**Files:**
- Create: `src/conference_scout/reminders/service.py`
- Create: `src/conference_scout/scheduler/jobs.py`, `setup.py`
- Test: `tests/unit/reminders/test_service.py`, `tests/unit/scheduler/test_setup.py`

**Interfaces:**
- Produces: `ReminderService.due_on(date, today, offsets)`, `process(owner, sender)`, `configure_scheduler(...)`.
- Consumes: verified and unverified future deadlines, subscriptions, reminder repository, configured timezone/schedules.

- [ ] **Step 1: Write failing reminder tests**

```python
def test_due_offset_uses_calendar_days():
    assert due_offset(date(2027, 2, 12), date(2027, 1, 29), (30, 14, 7)) == 14

async def test_failed_send_is_not_recorded(reminder_service, failing_sender):
    await reminder_service.process(failing_sender)
    assert await reminder_service.repository.count() == 0
```

Test duplicate suppression at both service and database levels.

- [ ] **Step 2: Verify RED**

Run: `pytest tests/unit/reminders tests/unit/scheduler -q`
Expected: missing reminder/scheduler modules.

- [ ] **Step 3: Implement reminders and APScheduler configuration**

Use calendar dates in configured timezone. Configure weekly/daily/hourly cron jobs with `coalesce=True`, `max_instances=1`, stable IDs, and replacement on startup.

- [ ] **Step 4: Verify background services and create major commit 3**

Run: `pytest tests/unit/reminders tests/unit/scheduler tests/integration/test_deadline_change.py -q && ruff check src tests`

Update `PROGRESS.md`, then run: `git add . && git commit -m "feat: monitor deadlines and schedule reminders"`

---

### Task 8: Owner-only Telegram interface

**Files:**
- Create: `src/conference_scout/bot/access.py`, `states.py`, `keyboards.py`, `messages.py`, `callbacks.py`
- Create: `src/conference_scout/bot/handlers/start.py`, `topics.py`, `sources.py`, `conferences.py`, `discovery.py`
- Create: `src/conference_scout/bot/router.py`, `notifications.py`
- Test: `tests/unit/bot/test_access.py`, `test_topics.py`, `test_sources.py`, `test_conferences.py`

**Interfaces:**
- Produces: root `Router`, `TelegramNotificationSender`, owner filter/middleware, FSM flows and callback data.
- Consumes: `OwnerContext`, topic/source/conference/discovery services, monitoring/reminder DTOs.

- [ ] **Step 1: Write failing authorization and handler tests**

```python
async def test_non_owner_update_is_rejected(owner_filter, message_factory):
    message = message_factory(user_id=999)
    assert await owner_filter(message) is False

async def test_topic_is_not_saved_before_confirmation(topic_handler):
    await topic_handler.preview("machine learning in stellar spectra")
    assert await topic_handler.repository.list_all() == []
```

Test topic preview/confirm/cancel, source validation and toggles, tracking, feedback, archives, details, and deadline ordering.

- [ ] **Step 2: Verify RED**

Run: `pytest tests/unit/bot -q`
Expected: missing bot modules.

- [ ] **Step 3: Implement owner gate and presentation helpers**

Reject unauthorized updates before handlers. Escape dynamic HTML, keep callback payloads bounded, and reveal no owner ID or configuration.

- [ ] **Step 4: Implement FSM and management handlers**

Wire main menu, topics, trusted sources, discovery acknowledgement/results, conference states/actions, and upcoming deadlines. Keep handlers thin and delegate state changes to services.

- [ ] **Step 5: Verify Telegram UX and create major commit 4**

Run: `pytest tests/unit/bot -q && pytest tests/integration -q && ruff check src tests`

Update `PROGRESS.md`, then run: `git add . && git commit -m "feat: add personal Telegram bot interface"`

---

### Task 9: Application lifecycle and healthcheck

**Files:**
- Create: `src/conference_scout/app.py`, `main.py`, `healthcheck.py`, `logging.py`
- Test: `tests/unit/test_app.py`, `tests/unit/test_healthcheck.py`, `tests/unit/test_logging.py`

**Interfaces:**
- Produces: `Application.create(settings)`, `Application.run()`, `python -m conference_scout.main`, healthcheck CLI.
- Consumes: all providers, services, owner bootstrap, bot router, scheduler.

- [ ] **Step 1: Write failing lifecycle tests**

Assert initialization order, graceful scheduler/bot shutdown, SQLite health success/failure, and redaction of secrets from structured log context.

- [ ] **Step 2: Verify RED**

Run: `pytest tests/unit/test_app.py tests/unit/test_healthcheck.py tests/unit/test_logging.py -q`
Expected: missing lifecycle modules.

- [ ] **Step 3: Implement composition root and healthcheck**

Build one shared async HTTP client per trust boundary, initialize schema/owner, register handlers/jobs, start polling, and close scheduler, clients, and engine on shutdown. Healthcheck performs only a lightweight SQLite query.

- [ ] **Step 4: Verify lifecycle**

Run: `pytest tests/unit/test_app.py tests/unit/test_healthcheck.py tests/unit/test_logging.py -q`
Expected: all selected tests pass.

---

### Task 10: Hardened containers and SearXNG configuration

**Files:**
- Create: `Dockerfile`, `compose.yaml`, `.dockerignore`
- Create: `deploy/searxng/settings.yml`, `deploy/searxng/limiter.toml`
- Test: `tests/unit/test_deployment_files.py`

**Interfaces:**
- Produces: `conference-scout` and internal `searxng` Compose services, persistent `conference_data` volume.
- Consumes: `.env`, application module and healthcheck CLI.

- [ ] **Step 1: Write deployment contract test**

Parse Compose YAML and assert no published ports, no Docker socket/host network/privileged mode, `read_only`, `/tmp` tmpfs, `cap_drop: ALL`, `no-new-privileges`, persistent `/data`, non-root image user, restart policy, and healthcheck.

- [ ] **Step 2: Verify RED**

Run: `pytest tests/unit/test_deployment_files.py -q`
Expected: deployment files are absent.

- [ ] **Step 3: Implement multi-stage image and Compose stack**

Install locked project dependencies, create non-root runtime user, copy only required code, configure SearXNG JSON format, use internal service discovery, and avoid host-published ports.

- [ ] **Step 4: Verify static deployment contract**

Run: `pytest tests/unit/test_deployment_files.py -q && docker compose config`
Expected: test passes and Compose renders successfully with placeholder environment.

---

### Task 11: GitHub-ready documentation and final verification

**Files:**
- Create: `README.md`, `LICENSE`
- Modify: `PROGRESS.md`, `.env.example`, `.gitignore`
- Test: `tests/unit/test_documentation.py`

**Interfaces:**
- Produces: complete operator/developer documentation matching implemented commands and configuration.
- Consumes: actual application behavior and deployment files.

- [ ] **Step 1: Write documentation contract test**

Assert README contains every required section, documented environment names exist in `.env.example`, startup commands match Compose, and backup instructions reference `/data/conference_scout.db`.

- [ ] **Step 2: Verify RED**

Run: `pytest tests/unit/test_documentation.py -q`
Expected: README/LICENSE are absent or incomplete.

- [ ] **Step 3: Write README and license**

Document personal single-owner deployment, Telegram bot creation, OpenRouter, SearXNG, configuration, trusted sources, topics, discovery, deadline tracking, backup/restore, security, development, tests, limitations, roadmap, and MIT license.

- [ ] **Step 4: Run full local quality suite**

Run: `ruff format --check . && ruff check . && pytest -q`
Expected: formatter, linter, unit tests, and integration tests all pass.

- [ ] **Step 5: Verify containers**

Run: `docker compose config && docker build -t conference-scout:test .`
Expected: Compose config succeeds and image builds successfully.

- [ ] **Step 6: Audit secrets and documentation accuracy**

Run targeted searches for token/key-like assignments and tracked `.env`/database files. Inspect `.gitignore`, effective Compose security fields, all configuration names, healthcheck command, README commands, and implemented Telegram actions. Any discovered issue gets a failing regression test before correction when code behavior changes.

- [ ] **Step 7: Update progress and create major commit 5**

Record exact verification commands/results and remaining limitations in `PROGRESS.md`.

Run: `git add . && git commit -m "docs: complete secure Docker deployment and documentation"`

- [ ] **Step 8: Inspect final repository state**

Run: `git status --short --branch && git log --oneline --decorate -6`
Expected: clean `main` branch with the design commit and five substantial implementation commits.
