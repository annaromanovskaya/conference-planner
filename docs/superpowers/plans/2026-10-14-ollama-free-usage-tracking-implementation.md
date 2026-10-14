# Ollama Free Usage Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add truthful, free-only Ollama Cloud usage tracking to ConfPlanner and expose local token totals plus authoritative-account links in Telegram settings.

**Architecture:** `ModelRegistry.chat` becomes the single metered chat boundary for Ollama Cloud, while `OllamaGateway` remains responsible for parsing counters and classifying HTTP failures. Daily aggregates and the latest provider status are stored through an owner-scoped repository; a focused usage service builds read-only snapshots for a dedicated Telegram settings router. Existing local Ollama and LLM Hub behavior remains unchanged.

**Tech Stack:** Python 3.12, Pydantic 2, httpx, SQLAlchemy 2 async, Alembic, aiogram 3, pytest

**Spec:** `docs/superpowers/specs/2026-10-14-ollama-free-usage-tracking-design.md`

## Global Constraints

- Track only profile `ollama-cloud` and only models in `OLLAMA_CLOUD_FREE_MODELS`.
- Never infer or display an account percentage, remaining credits, pool size, or billing-cycle reset from local token counters.
- Never scrape Ollama Settings, store browser credentials/cookies, buy credits, or introduce an automatic paid fallback.
- Persist no prompt text, response text, upstream response body, API key, cookie, or authentication redirect in usage tables.
- Missing or malformed Ollama counters remain unknown and are not converted to zero.
- A usage-write failure must not discard a successful model response or poison the caller's SQLAlchemy transaction.
- Model switching remains explicit; ConfPlanner never silently chooses another model after a limit failure.
- Preserve the existing configured free-model allowlist as the sole Ollama Cloud model list shown to users.

## Review Focus

- Ollama returns booleans, negative numbers, floats, strings, or integers above SQLite's signed 64-bit range as counters: ignore only those counters and preserve the successful chat response (Task 1 tests).
- Two owners or two models record usage on the same UTC day: aggregate into distinct rows and never leak one owner's totals into another snapshot (Task 2 tests).
- The aggregate upsert fails inside a transaction that already has domain changes: roll back only the savepoint and return the successful LLM response with the outer transaction still usable (Task 3 tests).
- `/api/tags` succeeds while the last chat was rate-limited or exhausted: refresh the available-model list without falsely replacing the last chat status with `AVAILABLE` (Task 4 tests).
- A model name contains HTML-significant characters and no counters have ever been recorded: escape it, state that observations are absent, and never render zeros as an account balance (Task 5 tests).

---

### Task 1: Ollama usage metadata and safe failure classification

**Files:**
- Modify: `src/conference_scout/ai/models.py`
- Modify: `src/conference_scout/ai/ollama.py`
- Modify: `tests/unit/ai/test_ollama.py`

**Interfaces:**
- Consumes: existing `ModelGatewayError(code: str)` and Ollama `/api/chat` response object.
- Produces: immutable optional `ChatResponse.prompt_tokens`, `cached_prompt_tokens`, and `output_tokens`; error codes `authentication`, `rate_limited`, `free_exhausted`, and `upstream_error`.

- [ ] **Step 1: Add failing parsing tests**

Add `test_ollama_parses_usage_counters` asserting `prompt_eval_count=120`, `prompt_eval_cached_count=80`, and `eval_count=25` become the three `ChatResponse` fields. Add parametrized `test_ollama_ignores_invalid_usage_counters` for `True`, `-1`, `1.5`, `"12"`, and `2**63`, asserting each malformed field is `None` while valid sibling fields remain populated. Add a missing-counter case asserting all three fields are `None`.

- [ ] **Step 2: Run parsing tests and verify they fail**

Run: `pytest tests/unit/ai/test_ollama.py -k "usage_counter" -v`

Expected: FAIL because `ChatResponse` has no usage fields.

- [ ] **Step 3: Add bounded optional counters**

In `ai/models.py`, add frozen `ChatResponse` fields with exact names `prompt_tokens`, `cached_prompt_tokens`, and `output_tokens`, each `int | None = None`. In `ai/ollama.py`, add `_usage_counter(payload: dict[str, Any], key: str) -> int | None`; accept non-boolean integers from `0` through `9_223_372_036_854_775_807` and otherwise return `None`.

- [ ] **Step 4: Run parsing tests and verify they pass**

Run: `pytest tests/unit/ai/test_ollama.py -k "usage_counter" -v`

Expected: PASS.

- [ ] **Step 5: Add failing HTTP classification tests**

Add parametrized `test_ollama_classifies_http_failures_without_leaking_body` for 401/403 → `authentication`, 402 → `free_exhausted`, 429 → `rate_limited`, and 500 → `upstream_error`. Assert neither the secret key nor response body appears in `str(error)`.

- [ ] **Step 6: Implement status classification and verify the gateway suite**

Update `OllamaGateway._request` to map only those status families to those exact codes and retain a sanitized message. Keep transport errors as `upstream_error`.

Run: `pytest tests/unit/ai/test_ollama.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/conference_scout/ai/models.py src/conference_scout/ai/ollama.py tests/unit/ai/test_ollama.py
git commit -m "feat: expose Ollama usage metadata"
```

### Task 2: Usage schema, migration, and owner-scoped repository

**Files:**
- Modify: `src/conference_scout/schemas/common.py`
- Modify: `src/conference_scout/db/models.py`
- Create: `src/conference_scout/db/repositories/ollama_usage.py`
- Create: `migrations/versions/20261014_0003_ollama_usage_tracking.py`
- Create: `tests/unit/db/test_ollama_usage_repository.py`
- Modify: `tests/integration/test_migrations.py`

**Interfaces:**
- Consumes: `ChatResponse` counters from Task 1 and an owner-scoped `AsyncSession`.
- Produces: `ProviderUsageStatus`; ORM models `OllamaUsageDaily` and `ProviderUsageState`; `OllamaUsageRepository.record_success(...)`, `record_status(...)`, `get_daily_totals(...)`, `get_month_totals(...)`, and `get_status(...)`.

- [ ] **Step 1: Write failing repository aggregation tests**

Create tests asserting `record_success(user_id, "ollama-cloud", model_name, response, observed_at)` creates one UTC-day row, a second request increments request/token counts, and missing counters do not add token values. Add isolation assertions for a second model, UTC day, and owner. Assert a response with all counters `None` creates no usage row but still records `AVAILABLE` with the observation time.

- [ ] **Step 2: Run repository tests and verify they fail**

Run: `pytest tests/unit/db/test_ollama_usage_repository.py -v`

Expected: FAIL because the models and repository do not exist.

- [ ] **Step 3: Define status and ORM models**

Add `ProviderUsageStatus(StrEnum)` with `AVAILABLE`, `RATE_LIMITED`, `FREE_EXHAUSTED`, `AUTH_ERROR`, and `UNKNOWN`. Add `OllamaUsageDaily` with the spec's columns and unique constraint `(user_id, profile_id, model_name, usage_date)`. Add `ProviderUsageState` mapped to table `provider_usage_status` with unique `(user_id, profile_id)` and sanitized `detail_code`.

- [ ] **Step 4: Implement the repository with a nested transaction**

Implement these signatures in `ollama_usage.py`:

```python
async def record_success(self, user_id: int, profile_id: str, model_name: str, response: ChatResponse, observed_at: datetime) -> None
async def record_status(self, user_id: int, profile_id: str, status: ProviderUsageStatus, *, model_name: str | None, observed_at: datetime, detail_code: str | None = None) -> None
async def get_daily_totals(self, user_id: int, profile_id: str, usage_date: date) -> tuple[OllamaUsageDaily, ...]
async def get_month_totals(self, user_id: int, profile_id: str, month_start: date, month_end: date) -> dict[str, UsageAggregate]
async def get_status(self, user_id: int, profile_id: str) -> ProviderUsageState | None
```

Use SQLite `INSERT ... ON CONFLICT DO UPDATE` inside `session.begin_nested()` for both unique keys. Increment only present counters; clamp aggregate arithmetic to signed 64-bit maximum. Never commit the caller's outer transaction.

- [ ] **Step 5: Run repository tests and verify they pass**

Run: `pytest tests/unit/db/test_ollama_usage_repository.py -v`

Expected: PASS.

- [ ] **Step 6: Add the Alembic migration and migration tests**

Create revision `20261014_0003` after `20261013_0002`. Update `AGENT_TABLES` in `test_migrations.py` with both usage tables; assert the daily unique constraint and provider-status unique constraint exist after upgrading an existing SQLite database containing a user/topic row.

- [ ] **Step 7: Verify migrations**

Run: `pytest tests/integration/test_migrations.py -v`

Expected: PASS for fresh, known legacy, and rejection of unknown partial schemas.

- [ ] **Step 8: Commit**

```bash
git add src/conference_scout/schemas/common.py src/conference_scout/db/models.py src/conference_scout/db/repositories/ollama_usage.py migrations/versions/20261014_0003_ollama_usage_tracking.py tests/unit/db/test_ollama_usage_repository.py tests/integration/test_migrations.py
git commit -m "feat: persist Ollama free usage totals"
```

### Task 3: One metered registry boundary for every cloud chat

**Files:**
- Modify: `src/conference_scout/ai/registry.py`
- Modify: `src/conference_scout/ai/active_provider.py`
- Modify: `src/conference_scout/agent/runtime.py`
- Modify: `tests/unit/ai/test_registry.py`
- Modify: `tests/unit/agent/test_runtime.py`
- Modify: `tests/unit/ai/test_service.py`

**Interfaces:**
- Consumes: `OllamaUsageRepository` from Task 2 and Task 1 error codes.
- Produces: `ModelRegistry.chat(user_id, active, messages, tools=(), *, response_schema=None) -> ChatResponse`; all agent, summary, structured-output, and capability-probe calls use it.

- [ ] **Step 1: Write failing registry boundary tests**

Extend `test_registry.py` with `test_cloud_chat_records_usage`, `test_non_cloud_chat_is_not_recorded`, and a parametrized failure test mapping gateway error codes to the five persisted statuses. Assert the original `ModelGatewayError` is re-raised after status recording.

- [ ] **Step 2: Add the failing savepoint-isolation test**

Use a recorder double whose `record_success` raises after a successful gateway response. Assert `registry.chat(...)` still returns that response, logs the write error, and the test can flush/query a previously added domain row in the same outer session.

- [ ] **Step 3: Run registry tests and verify they fail**

Run: `pytest tests/unit/ai/test_registry.py -k "cloud_chat or non_cloud_chat or isolation or records_status" -v`

Expected: FAIL because `ModelRegistry.chat` and recorder injection do not exist.

- [ ] **Step 4: Implement the metered boundary**

Accept `usage_repository: OllamaUsageRepository | None = None` in `ModelRegistry.__init__` and `for_testing`. Add `chat(...)` with the exact signature above. For `ollama-cloud`, call `record_success` after successful chat; on `ModelGatewayError`, map `authentication` → `AUTH_ERROR`, `rate_limited` → `RATE_LIMITED`, `free_exhausted` → `FREE_EXHAUSTED`, everything else → `UNKNOWN`, record sanitized `detail_code`, and re-raise. Catch and log recorder failures without replacing a successful response.

- [ ] **Step 5: Route capability probes through `ModelRegistry.chat`**

Replace direct `gateway.chat` in `refresh_capability` with `self.chat(user_id, active, ...)`, preserving the existing timeout and capability persistence.

- [ ] **Step 6: Route runtime and structured calls through the boundary**

Update `ActiveLLMProvider.complete` and both `AgentRuntime` call sites (normal step and summarization callback) to call `registry.chat`. Remove the now-unneeded gateway parameter from `_finish`. In `AgentRuntime`, map `free_exhausted` to `Ollama Free usage исчерпан. Выберите другую бесплатную модель через /model.` and `rate_limited` to `Ollama временно ограничила запросы. Попробуйте позже или выберите другую бесплатную модель через /model.`; keep other errors generic.

- [ ] **Step 7: Add call-path regression tests**

In runtime tests, assert both the main reply and forced compaction use `registry.chat`. In AI service tests, assert structured completion preserves `response_schema` through `registry.chat`. Retain the capability-probe usage assertion in `test_registry.py`.

- [ ] **Step 8: Verify all metered call paths**

Run: `pytest tests/unit/ai/test_registry.py tests/unit/agent/test_runtime.py tests/unit/ai/test_service.py -v`

Expected: PASS, including main chat, summarization, structured AI, and capability probe.

- [ ] **Step 9: Commit**

```bash
git add src/conference_scout/ai/registry.py src/conference_scout/ai/active_provider.py src/conference_scout/agent/runtime.py tests/unit/ai/test_registry.py tests/unit/agent/test_runtime.py tests/unit/ai/test_service.py
git commit -m "feat: meter Ollama calls at model registry"
```

### Task 4: Truthful usage snapshots and model availability refresh

**Files:**
- Create: `src/conference_scout/ai/usage.py`
- Create: `tests/unit/ai/test_usage.py`
- Modify: `src/conference_scout/ai/registry.py`
- Modify: `tests/unit/ai/test_registry.py`

**Interfaces:**
- Consumes: repository reads from Task 2, `ModelRegistry.get_active`, and allowlist-filtered `ModelRegistry.list_models`.
- Produces: frozen `ModelUsageView`, `OllamaUsageSnapshot`, and `OllamaUsageService.snapshot(user_id, *, refresh_models: bool) -> OllamaUsageSnapshot`; `ModelRegistry.allowed_models(profile_id) -> tuple[str, ...]`.

- [ ] **Step 1: Write failing snapshot tests**

Cover no observations, two configured models with independent today/month totals, active LLM Hub selection, all five statuses, and UTC month boundaries. Assert every configured free model appears even with no local row, and totals are explicitly local observations.

- [ ] **Step 2: Write failing refresh semantics tests**

Assert `refresh_models=True` calls the allowlist-filtered `/api/tags` path and returns only configured names present upstream. Assert a successful list refresh does not overwrite a stored `RATE_LIMITED` or `FREE_EXHAUSTED` chat status. Assert list failure records the classified provider status but returns a snapshot with an empty available-model set instead of losing saved totals.

- [ ] **Step 3: Run usage-service tests and verify they fail**

Run: `pytest tests/unit/ai/test_usage.py -v`

Expected: FAIL because the service does not exist.

- [ ] **Step 4: Implement snapshot data types and service**

Define immutable views with model name, today request/input/cached/output totals, month totals, whether any counters were observed, active provider/model, provider status/detail, observation timestamp, and currently available allowlisted cloud models. The service must use UTC dates and must not calculate percentage, remaining balance, or a reset date.

- [ ] **Step 5: Add allowlist access without exposing discovered paid models**

Implement `ModelRegistry.allowed_models("ollama-cloud")` from `ProviderProfile.allowed_models`. Keep `list_models` filtering in configured order. Add `user_id: int | None = None` to `list_models`; only when provided, persist classified cloud list failures, never mark a chat status available from `/api/tags` success.

- [ ] **Step 6: Verify usage and registry tests**

Run: `pytest tests/unit/ai/test_usage.py tests/unit/ai/test_registry.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/conference_scout/ai/usage.py src/conference_scout/ai/registry.py tests/unit/ai/test_usage.py tests/unit/ai/test_registry.py
git commit -m "feat: build Ollama free usage snapshots"
```

### Task 5: Telegram settings screen and isolated callback handlers

**Files:**
- Create: `src/conference_scout/bot/usage_router.py`
- Create: `tests/unit/bot/test_usage_router.py`
- Modify: `src/conference_scout/bot/keyboards.py`
- Modify: `src/conference_scout/bot/router.py`
- Modify: `tests/unit/bot/test_router.py`

**Interfaces:**
- Consumes: `OllamaUsageService`, `OllamaUsageSnapshot`, existing `list_model_profiles`, injected `owner_user_id`, and FSM state.
- Produces: `build_usage_router()`, `format_usage_snapshot(snapshot) -> str`, and `ollama_usage_keyboard(*, offer_model_choice: bool) -> InlineKeyboardMarkup`; exact callback prefixes `usage:refresh` and `usage:model`.

- [ ] **Step 1: Write failing rendering tests**

Assert settings text contains the active provider/model, `OLLAMA FREE USAGE`, one row per configured model, today's request/input/cached/output counts, local calendar-month totals, translated status, and observation time. For no data, assert `Локальных наблюдений пока нет` and absence of `%`, `осталось`, and an account-balance claim. Assert HTML-significant model names are escaped.

- [ ] **Step 2: Write failing keyboard and callback tests**

Assert the keyboard always has `Обновить статистику` and URL `https://ollama.com/settings/usage`; add `Выбрать другую модель` only for `RATE_LIMITED`/`FREE_EXHAUSTED` when another allowlisted model is currently available. Assert refresh rerenders settings and answers the callback. Assert model choice delegates to the existing model-selection flow. Assert unrelated callback data reaches the existing stale-action fallback, not the usage handlers.

- [ ] **Step 3: Run bot usage tests and verify they fail**

Run: `pytest tests/unit/bot/test_usage_router.py tests/unit/bot/test_router.py -v`

Expected: FAIL because the router and keyboard do not exist.

- [ ] **Step 4: Implement the formatter and keyboard**

Render plain Russian text with numbered model rows and HTML only for safe emphasis; escape every external/model value. Label month totals as local observations and state that exact Free usage is only in Ollama Settings. Do not use Markdown or derive a percentage.

- [ ] **Step 5: Implement narrowly filtered handlers**

Move the `Настройки` message handler into `usage_router.py`; call `open_menu`, request `snapshot(..., refresh_models=True)`, and answer with the usage keyboard. Register exact callbacks `F.data == "usage:refresh"` and `F.data == "usage:model"`; clear stale FSM state before invoking model selection. Include this router before the callback fallback.

- [ ] **Step 6: Verify bot routing**

Run: `pytest tests/unit/bot/test_usage_router.py tests/unit/bot/test_model_router.py tests/unit/bot/test_router.py -v`

Expected: PASS; each button has one handler and unknown callbacks still report that the action is stale.

- [ ] **Step 7: Commit**

```bash
git add src/conference_scout/bot/usage_router.py src/conference_scout/bot/keyboards.py src/conference_scout/bot/router.py tests/unit/bot/test_usage_router.py tests/unit/bot/test_router.py
git commit -m "feat: show Ollama free usage in Telegram"
```

### Task 6: Application wiring for bot, jobs, and MCP-backed workflows

**Files:**
- Modify: `src/conference_scout/app.py`
- Modify: `tests/integration/test_agent_application.py`
- Modify: `tests/integration/test_discovery.py`
- Modify: `tests/integration/test_monitoring.py`

**Interfaces:**
- Consumes: `OllamaUsageRepository`, `OllamaUsageService`, and metered `ModelRegistry` from Tasks 2–4.
- Produces: every request-scoped and scheduled `ModelRegistry` receives an owner-scoped usage repository; middleware injects `ollama_usage_service` into bot handlers.

- [ ] **Step 1: Add failing wiring/integration tests**

Assert a normal agent response records one cloud request, forced summarization records its extra request, a discovery structured extraction records usage, and a monitoring structured extraction records usage. Assert LLM Hub operations leave Ollama usage tables unchanged.

- [ ] **Step 2: Run focused integration tests and verify they fail**

Run: `pytest tests/integration/test_agent_application.py tests/integration/test_discovery.py tests/integration/test_monitoring.py -k "ollama_usage" -v`

Expected: FAIL because production registries are not wired to the repository.

- [ ] **Step 3: Centralize registry construction in `app.py`**

Add a small `_model_registry(session, profiles, *, capability_timeout=20) -> ModelRegistry` factory that always passes `AgentStateRepository(session)` and `OllamaUsageRepository(session)`. Replace every direct `ModelRegistry(...)` construction in `app.py`: request-scoped bot services, discovery job, monitoring job, and MCP read handlers.

- [ ] **Step 4: Inject the settings service**

In the request-scoped `services` factory, add `ollama_usage_service=OllamaUsageService(OllamaUsageRepository(session), registry)` using the same session and registry. Do not add any new secret, cookie, or paid-account configuration.

- [ ] **Step 5: Verify integration paths**

Run: `pytest tests/integration/test_agent_application.py tests/integration/test_discovery.py tests/integration/test_monitoring.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/conference_scout/app.py tests/integration/test_agent_application.py tests/integration/test_discovery.py tests/integration/test_monitoring.py
git commit -m "feat: wire Ollama usage tracking across services"
```

### Task 7: Operator documentation and complete verification

**Files:**
- Modify: `README.md`
- Modify: `.env.example`

**Interfaces:**
- Consumes: completed Telegram UX and free-only behavior.
- Produces: deployment instructions that distinguish local observations from Ollama's authoritative Free usage percentage.

- [ ] **Step 1: Document the operator workflow**

Explain that `Настройки` shows locally observed per-model request/token totals, `Точный Free usage в Ollama` opens the authoritative page, 429 means temporary rate limiting rather than proven exhaustion, and only `OLLAMA_CLOUD_FREE_MODELS` is offered. State explicitly that ConfPlanner cannot read the account percentage or prevent billing if paid credits are enabled in the Ollama account.

- [ ] **Step 2: Verify environment documentation remains free-only**

Keep `OLLAMA_CLOUD_API_KEY`, `OLLAMA_CLOUD_BASE_URL`, and `OLLAMA_CLOUD_FREE_MODELS`; add no billing, login/password, cookie, or scraping variables.

- [ ] **Step 3: Run formatting, lint, and the full test suite**

Run: `ruff format --check src tests`

Expected: exit 0.

Run: `ruff check src tests`

Expected: exit 0.

Run: `pytest -q`

Expected: all tests pass.

- [ ] **Step 4: Inspect the final diff for secrets and scope**

Run: `git diff --check`

Expected: no whitespace errors.

Run: `git diff -- . ':!docs/superpowers/specs/2026-10-14-ollama-free-usage-tracking-design.md' | rg -n "hub_|Bearer |cookie|password|[0-9]+\\.[0-9]+% used"`

Expected: no real secret, credential, scraped percentage, or hard-coded account usage value.

- [ ] **Step 5: Commit documentation**

```bash
git add README.md .env.example
git commit -m "docs: explain Ollama free usage tracking"
```
