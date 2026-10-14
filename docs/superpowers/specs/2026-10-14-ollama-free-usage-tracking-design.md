# Ollama Free Usage Tracking Design

## Purpose

ConfPlanner must help its single Telegram owner understand how much Ollama Cloud is being used without enabling or implying paid usage. The bot will record usage reported by Ollama for every cloud chat completion, summarize it per free model, detect availability and limit-related failures, and link to Ollama's authenticated usage page for the authoritative account-wide percentage.

The feature must never invent a remaining percentage or credit balance. Ollama publishes token counts in chat responses but does not document an API for the account percentage shown at `https://ollama.com/settings/usage`.

## Scope

The feature covers only the `ollama-cloud` provider and only models present in `OLLAMA_CLOUD_FREE_MODELS`. Local Ollama and LLM Hub usage is not billed or summarized by this feature. ConfPlanner will not scrape authenticated web pages, store Ollama browser credentials or cookies, purchase credits, or select paid models.

The application cannot prevent account-level billing if the owner later enables paid credits directly in Ollama. Its enforceable boundary is that it exposes and selects only the configured free-model allowlist and never performs a paid fallback.

## User Experience

The existing Telegram button `Настройки` becomes the entry point. Its response contains:

1. The selected provider and model.
2. An `OLLAMA FREE USAGE` section when Ollama Cloud is configured.
3. One row per configured free model with today's request count and input, cached-input, and output token totals.
4. Account-wide status: `доступен`, `временно ограничен`, `Free usage исчерпан`, `ошибка авторизации`, or `неизвестно`.
5. The timestamp of the latest successful request or status observation.
6. A clear note that the exact percentage remains available only in Ollama Settings.

The response has inline buttons:

- `Обновить статистику` rerenders data already known locally and refreshes model availability through the supported Ollama model-list endpoint.
- `Точный Free usage в Ollama` opens `https://ollama.com/settings/usage`.
- When the selected model is limited but another configured free model is currently available, `Выбрать другую модель` opens the existing model-selection flow. ConfPlanner does not switch models silently.

When no usage has been recorded, the bot says so rather than displaying zeros as an account balance. Monthly local totals are calendar-month observations, not Ollama's billing-cycle percentage, because the Free reset date and pool size are not available through the documented API.

## Data Model

Add a daily aggregate table `ollama_usage_daily`:

- `id`: primary key.
- `user_id`: owner foreign key.
- `profile_id`: fixed to `ollama-cloud` for this feature.
- `model_name`: exact Ollama API model name.
- `usage_date`: UTC calendar date.
- `request_count`: completed chat requests.
- `prompt_tokens`: sum of `prompt_eval_count`.
- `cached_prompt_tokens`: sum of `prompt_eval_cached_count`.
- `output_tokens`: sum of `eval_count`.
- `created_at` and `updated_at`.
- Unique constraint on `(user_id, profile_id, model_name, usage_date)`.

Add a provider-status table `provider_usage_status`:

- `id`: primary key.
- `user_id`, `profile_id` with a unique constraint.
- `status`: `AVAILABLE`, `RATE_LIMITED`, `FREE_EXHAUSTED`, `AUTH_ERROR`, or `UNKNOWN`.
- `model_name`: model involved in the last observation, nullable.
- `observed_at`: UTC timestamp.
- `detail_code`: sanitized internal classification only; no upstream response body.

Daily aggregation avoids unbounded per-request rows. Writes occur in the same owner-scoped SQLAlchemy session used by the caller and use an upsert-safe repository operation.

## Provider Contract

Extend `ChatResponse` with optional immutable usage metadata:

- `prompt_tokens`
- `cached_prompt_tokens`
- `output_tokens`

`OllamaGateway.chat` reads these values from `prompt_eval_count`, `prompt_eval_cached_count`, and `eval_count`. Missing counters remain `None`; they are not treated as zero. Other gateways may leave usage absent.

Classify Ollama HTTP failures without exposing bodies:

- HTTP 401 or 403: `AUTH_ERROR`.
- HTTP 429: `RATE_LIMITED`; this is not automatically called quota exhaustion.
- HTTP 402, or an explicitly documented quota/credit exhaustion code if Ollama adds one: `FREE_EXHAUSTED`.
- Other transport and 5xx failures: `UNKNOWN`.

The classification is provider state, not per-model balance. Free credits are an account pool; per-model rows are local consumption statistics.

## Recording Flow

All Ollama Cloud calls must pass through one recording boundary so agent replies, structured extraction, relevance ranking, capability probes, and conversation summarization are counted consistently.

`ModelRegistry` owns the recording operation because it knows the owner, profile, model, and request-scoped database repository. After every successful `gateway.chat`, callers pass the returned `ChatResponse` to the registry. The registry records only `ollama-cloud` responses with at least one reported counter and marks the provider available. On a classified Ollama failure, the same boundary records the provider status before the existing user-safe error handling continues.

The implementation must cover all current call sites:

- `AgentRuntime` agent steps and summarization.
- `ActiveLLMProvider` structured completion calls.
- `ModelRegistry.refresh_capability`.

No prompt or response content is stored in usage tables.

## Free-Only Enforcement

The existing `OLLAMA_CLOUD_FREE_MODELS` allowlist remains the sole list exposed by `/model`. If a configured model disappears from `/api/tags`, it is unavailable for selection. The feature does not discover and automatically add unlisted cloud models.

On `FREE_EXHAUSTED`, ConfPlanner stops retrying the same cloud request and returns a deterministic Russian message stating that Ollama Free usage is exhausted. On `RATE_LIMITED`, it reports a temporary restriction and recommends retrying later. It may offer another currently listed free model, but selection always requires the owner's action.

## Reliability and Security

- Usage recording failure must not discard an otherwise successful model response; it is logged and the database session is recovered.
- Token counters are accepted only as non-negative integers with bounded database values.
- Upstream error bodies, API keys, cookies, and authentication redirects are never persisted or shown.
- The authenticated settings page is opened by Telegram as an external URL; ConfPlanner never authenticates to it.
- Status refresh has the same bounded HTTP timeout as existing model discovery.
- Local totals are labeled as local observations and never presented as the authoritative Ollama percentage.

## Testing

Automated tests must cover:

- Ollama token-counter parsing, including missing and malformed counters.
- HTTP status classification for 401/403, 402, 429, and 5xx.
- Daily aggregate creation and increment without cross-user or cross-model mixing.
- Recording from agent, structured AI, summarization, and capability-probe paths.
- Recording failure isolation from successful chat responses.
- Settings rendering with no data, multiple models, each provider status, and escaped model names.
- Inline URL and refresh/model-selection buttons.
- Free-model allowlist enforcement and absence of automatic paid fallback.
- Database migration upgrade on an existing SQLite database.

## Acceptance Criteria

The feature is complete when the owner can open `Настройки`, see truthful per-model Ollama Cloud token usage and current Free availability, open Ollama's authoritative usage page, and choose another configured free model after a limit failure. No screen may claim an exact remaining percentage or credit balance derived from local counters, and no paid model or paid fallback may be introduced.
