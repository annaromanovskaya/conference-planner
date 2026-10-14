# Verified Conference Discovery and Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make conversational research produce verified, persisted conference candidates and automatically maintained exact or approximate deadlines reflected in Telegram.

**Architecture:** The selected model orchestrates typed MCP tools while separate structured passes extract and verify evidence. A deterministic persistence service owns source precedence, transactions, and read-back receipts; scheduled and manual monitoring use the same pipeline.

**Tech Stack:** Python 3.12, aiogram 3, SQLAlchemy 2 async, Alembic, Pydantic 2, APScheduler, MCP v2, httpx, SQLite, pytest.

**Spec:** `docs/superpowers/specs/2026-10-14-verified-conference-monitoring-design.md`

## Global Constraints

- New conferences become tracked only after `Добавить`; tracked facts and deadlines then update automatically.
- Only a committed read-back receipt permits `сохранено` or `обновлено` wording.
- Source precedence is official conference, official organizer, then reputable secondary evidence.
- Month-only deadlines remain approximate periods; never invent a day.
- Every persisted claim carries URL, source class, bounded excerpt, observation time, and verification state.
- TLS verification, SSRF protection, bounded retries, owner isolation, and transaction atomicity remain mandatory.
- Existing owner data must upgrade without destructive recreation.

## Review Focus

- Candidate callbacks after process restart resolve durable state or return a precise stale message; Task 4 pins this.
- Recovered official evidence conflicting with secondary data preserves history and wins precedence; Task 3 pins this.
- Approximate deadlines crossing years sort correctly but never trigger exact-day reminders; Task 7 pins this.
- Concurrent scheduled and manual checks remain idempotent; Task 5 pins this.
- Large malicious catalog pages remain bounded and cannot inject unsupported claims; Task 2 pins this.

---

### Task 1: Evidence, Candidate, Deadline, and Attempt Storage

**Files:**
- Modify: `src/conference_scout/schemas/common.py`
- Modify: `src/conference_scout/db/models.py`
- Create: `src/conference_scout/db/repositories/conference_research.py`
- Create: `migrations/versions/20261014_0004_verified_monitoring.py`
- Modify: `tests/integration/test_migrations.py`
- Create: `tests/unit/db/test_verified_monitoring_repository.py`

**Interfaces:**
- Consumes: existing conference, deadline, history, and owner records.
- Produces: `VerificationStatus`, `EvidenceSourceClass`, `CandidateStatus`, `MonitoringAttemptStatus`; models for aliases, evidence, durable candidates, generic changes, and attempts; extended exact/approximate deadline fields.

- [ ] **Step 1: Write failing migration tests** asserting upgrade from `20261014_0003` preserves a tracked conference and adds five new tables, candidate indexes, and nullable exact-date plus approximate year/month fields.
- [ ] **Step 2: Run `uv run pytest tests/integration/test_migrations.py -q -p no:cacheprovider`**; expect failure because revision and columns do not exist.
- [ ] **Step 3: Add enums and ORM fields**. Extend `DeadlineType` with registration opening, oral abstract, acceptance notification, poster submission, and payment; validate either exact date or approximate year and month 1–12.
- [ ] **Step 4: Implement migration `20261014_0004`** with SQLite batch alteration, preserving exact dates and mapping legacy `verified=True` to official confirmation.
- [ ] **Step 5: Write repository tests** for owner isolation, aliases, bounded evidence, approximate periods, candidate status transitions, attempts, and change records.
- [ ] **Step 6: Implement repository methods** `create_candidates`, `get_candidate`, `set_candidate_status`, `list_candidates`, `record_attempt`, and evidence/history readers.
- [ ] **Step 7: Run Task 1 tests**; expect all pass.
- [ ] **Step 8: Commit** with `git commit -m "feat: add verified monitoring data model"`.

### Task 2: Evidence Extraction and Independent Verification

**Files:**
- Create: `src/conference_scout/schemas/research.py`
- Modify: `src/conference_scout/ai/prompts.py`
- Modify: `src/conference_scout/ai/service.py`
- Create: `tests/unit/ai/test_research_verification.py`

**Interfaces:**
- Consumes: Task 1 verification/source enums and existing structured completion.
- Produces: `FactClaim`, `DeadlineClaim`, `ConferenceResearch`, `VerifiedConferenceResearch`; `extract_conference_research(source_url, text, expected_name)` and `verify_conference_research(research)`.

- [ ] **Step 1: Write failing extraction tests** for exact dates, IMBASE month-only tentative deadlines, required source/excerpt, target selection from a catalog, and bounded malicious page content.
- [ ] **Step 2: Write failing verifier tests** proving unsupported claims become `UNVERIFIABLE`, tentative wording becomes `TENTATIVE`, and conflicting claims remain separate.
- [ ] **Step 3: Run focused tests** and expect missing schema/method failures.
- [ ] **Step 4: Implement closed Pydantic schemas** with bounded excerpts and collections, normalized URLs, and exact-or-approximate deadline validation.
- [ ] **Step 5: Add separate extractor and verifier prompts** treating all pages as untrusted and forbidding outside knowledge.
- [ ] **Step 6: Implement both AI methods** using bounded retries and provider JSON schema; keep legacy callers until Task 5.
- [ ] **Step 7: Run research and existing AI service tests**; expect all pass.
- [ ] **Step 8: Commit** with `git commit -m "feat: add evidence extraction and verification"`.

### Task 3: Deterministic Persistence and Receipts

**Files:**
- Create: `src/conference_scout/conferences/persistence.py`
- Modify: `src/conference_scout/db/repositories/conferences.py`
- Create: `tests/integration/test_conference_persistence.py`

**Interfaces:**
- Consumes: `VerifiedConferenceResearch` and Task 1 models.
- Produces: `PersistenceReceipt`, `PersistedDeadlineView`, and `ConferencePersistenceService.persist(user_id, verified, *, track: bool) -> PersistenceReceipt`.

- [ ] **Step 1: Write failing receipt tests** asserting one transaction creates facts, evidence, exact/approximate deadlines, optional subscription, and reread IDs.
- [ ] **Step 2: Write failing precedence tests**: official replaces secondary with history; secondary cannot replace official; repeated input is idempotent; forced failure rolls everything back.
- [ ] **Step 3: Run Task 3 tests**; expect missing service failures.
- [ ] **Step 4: Implement matching and persistence** by canonical URL, normalized name, and aliases; lock matches, upsert evidence/deadlines, record changes, reread, then return receipt.
- [ ] **Step 5: Add deterministic `PersistenceReceipt.telegram_summary()`** listing only actual created/updated fields.
- [ ] **Step 6: Run persistence and existing repository tests**; expect all pass.
- [ ] **Step 7: Commit** with `git commit -m "feat: persist verified conference receipts"`.

### Task 4: Durable Candidate Research

**Files:**
- Create: `src/conference_scout/conferences/research.py`
- Modify: `src/conference_scout/conferences/discovery.py`
- Modify: `src/conference_scout/app.py`
- Modify: `src/conference_scout/schemas/agent.py`
- Create: `tests/integration/test_candidate_research.py`

**Interfaces:**
- Consumes: extractor/verifier, candidate repository, persistence service.
- Produces: `ConferenceResearchService.search(user_id, query, limit)`, `inspect(user_id, candidate_id)`, `add(user_id, candidate_id) -> PersistenceReceipt`; durable integer candidate IDs.

- [ ] **Step 1: Write failing tests** for source-ranked deduplicated candidates, inspect without tracking, Add with tracked receipt, Skip, and cross-owner denial.
- [ ] **Step 2: Add restart test** resolving a candidate from a new session plus precise expired/skipped/imported errors.
- [ ] **Step 3: Run Task 4 tests**; expect process-local candidate behavior to fail.
- [ ] **Step 4: Implement research service** using SearXNG, safe fetch, two-pass verification, durable candidates, fallback sources, and strict limits.
- [ ] **Step 5: Replace `CandidateStore` wiring** and update action schemas so search ranks cannot masquerade as IDs.
- [ ] **Step 6: Run candidate, discovery, and agent application tests**; expect all pass.
- [ ] **Step 7: Commit** with `git commit -m "feat: add durable conference candidates"`.

### Task 5: Automatic Bootstrap and Monitoring

**Files:**
- Rewrite: `src/conference_scout/conferences/monitoring.py`
- Modify: `src/conference_scout/app.py`
- Modify: `src/conference_scout/scheduler/setup.py`
- Modify: `tests/integration/test_monitoring.py`

**Interfaces:**
- Consumes: research/persistence services and tracked subscriptions.
- Produces: `check_conference(user_id, conference_id) -> PersistenceReceipt` and `run(user_id) -> MonitoringReport` with created, changed, conflict, unchanged, and sanitized failure outcomes.

- [ ] **Step 1: Write failing bootstrap tests** proving an empty tracked placeholder receives event facts, exact and approximate deadlines, evidence, last-success time, and a receipt.
- [ ] **Step 2: Write fallback tests** for TLS failure, organizer/secondary evidence, later official recovery, prolonged failure, and stored-name catalog targeting.
- [ ] **Step 3: Add concurrent manual/scheduled test** proving one deadline set and no duplicate history.
- [ ] **Step 4: Run monitoring tests**; verify the current update-only implementation fails.
- [ ] **Step 5: Implement unified monitoring** with per-conference idempotency, fallback research, verification, persistence, attempt records, and no-change detection.
- [ ] **Step 6: Update scheduled notifications** to report created/changed facts, conflicts, or prolonged failure; log sanitized errors and keep no-change runs silent.
- [ ] **Step 7: Run monitoring, persistence, and scheduler tests**; expect all pass.
- [ ] **Step 8: Commit** with `git commit -m "feat: bootstrap tracked conference monitoring"`.

### Task 6: MCP Orchestration and Honest Responses

**Files:**
- Modify: `src/conference_scout/mcp/server.py`
- Modify: `src/conference_scout/mcp/tools.py`
- Modify: `src/conference_scout/agent/prompts.py`
- Modify: `src/conference_scout/agent/runtime.py`
- Modify: `src/conference_scout/app.py`
- Modify: `tests/integration/test_mcp_server.py`
- Modify: `tests/unit/agent/test_runtime.py`

**Interfaces:**
- Consumes: candidate and monitoring services plus receipts.
- Produces: tools `search_conference_candidates`, `get_candidate`, `propose_add_candidate`, `list_conference_details`, `check_tracked_conference`; receipt-aware response policy.

- [ ] **Step 1: Write failing MCP tests** for closed schemas, owner scoping, candidate confirmation, checks restricted to tracked conferences, and bounded receipts.
- [ ] **Step 2: Write runtime tests** where unsupported model text saying `обновлено` is replaced by honest copy, while a real receipt permits deterministic success.
- [ ] **Step 3: Run focused MCP/runtime tests**; expect failures.
- [ ] **Step 4: Implement tool handlers and receipt policy**; application code, never model prose, generates mutation success messages.
- [ ] **Step 5: Update system prompt** to distinguish found, proposed, verified, and persisted states.
- [ ] **Step 6: Run MCP, runtime, and agent integration tests**; expect all pass.
- [ ] **Step 7: Commit** with `git commit -m "feat: orchestrate verified conference tools"`.

### Task 7: Telegram Candidates, Lists, Cards, and Deadlines

**Files:**
- Create: `src/conference_scout/bot/conference_router.py`
- Modify: `src/conference_scout/bot/keyboards.py`
- Modify: `src/conference_scout/bot/router.py`
- Modify: `src/conference_scout/db/repositories/reminders.py`
- Create: `tests/unit/bot/test_conference_router.py`
- Modify: `tests/unit/bot/test_router.py`
- Modify: `tests/unit/db/test_repositories.py`

**Interfaces:**
- Consumes: durable candidates, persisted details, deadline queries, manual monitoring.
- Produces callbacks `candidate:add:{id}`, `candidate:details:{id}`, `candidate:skip:{id}`, `conference:details:{id}`, `conference:check:{id}`, `conference:history:{id}` and paginated database-backed views.

- [ ] **Step 1: Write rendering tests** for escaped cards/buttons, tracked/candidate/archive lists, source/history details, and precise stale alerts.
- [ ] **Step 2: Write deadline tests** ordering exact then approximate periods, labeling tentative values, excluding past periods, and preventing approximate reminders from pretending to be exact days.
- [ ] **Step 3: Write manual-check tests** for progress acknowledgement, receipt result, conflicts, and sanitized failures.
- [ ] **Step 4: Run focused bot/repository tests**; expect failures.
- [ ] **Step 5: Implement conference router/keyboards**, register before callback fallback, and remove duplicate root handlers.
- [ ] **Step 6: Implement database projections** so lists/cards never read conversation claims.
- [ ] **Step 7: Run all bot/repository tests**; expect all pass.
- [ ] **Step 8: Commit** with `git commit -m "feat: add conference monitoring views"`.

### Task 8: IMBASE Acceptance and Complete Verification

**Files:**
- Create: `tests/integration/test_imbase_monitoring.py`
- Modify: `README.md`
- Modify: `.env.example` only for existing bounded settings; add no TLS bypass or secrets.

**Interfaces:**
- Consumes: complete pipeline.
- Produces: executable IMBASE acceptance coverage and operator documentation.

- [ ] **Step 1: Add IMBASE fixture/test** asserting exact 12–16 July 2027 dates, four tentative month periods, Tenerife location, evidence URLs, receipt IDs, conference card, and deadline list.
- [ ] **Step 2: Add failure acceptance tests** for unavailable official page with labeled secondary fallback and rollback preventing false success copy.
- [ ] **Step 3: Run `uv run pytest tests/integration/test_imbase_monitoring.py -q -p no:cacheprovider`**; expect all pass.
- [ ] **Step 4: Document candidate approval, automatic updates, manual checking, source precedence, approximate deadlines, and diagnostics.**
- [ ] **Step 5: Run `uv run ruff format --check src tests` and `uv run ruff check src tests`**; expect zero issues.
- [ ] **Step 6: Run `uv run pytest -q -p no:cacheprovider`**; expect full suite pass.
- [ ] **Step 7: Run `git diff --check` and scan changed/untracked files excluding `.env`** for credentials, TLS-disable flags, invented IMBASE days, and unsupported success copy.
- [ ] **Step 8: Commit** with `git commit -m "docs: explain verified conference monitoring"`.
