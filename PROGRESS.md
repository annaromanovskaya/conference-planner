# ConfPlanner Progress

Last updated: 2026-10-13

## Current stage

Conversational agent, persistent memory, internal MCP, Ollama/local model selection, Telegram
confirmations, migrations and secure deployment are implemented on `main`.

## Completed

- Replaced the runtime dependency on OpenRouter with provider-neutral Ollama Cloud, local Ollama,
  and local OpenAI-compatible gateways.
- Added persisted profile/model selection and tool-calling capability checks.
- Added Alembic migration support for fresh and legacy SQLite databases without destructive schema
  replacement.
- Added persisted conversations, compact summaries, scoped user memories, provenance and an SQL
  knowledge-graph projection.
- Added seven validated pending-action types with owner binding, expiry, cancellation, retryable
  failure and exactly-once completed-state checks.
- Added an internal loopback-only MCP v2 Streamable HTTP server with ten read tools and seven
  proposal tools; no shell/filesystem/raw SQL/generic web authority is exposed.
- Added a bounded agent runtime with schema validation, step/call/timeout/result limits and safe
  provider/tool error normalization.
- Made ordinary Telegram text the primary AI chat; added `/model`, `/new`, `/memory`, `/forget`
  and generalized confirmation callbacks.
- Wired migrations, gateways, MCP, Telegram, scheduler and graceful reverse-order shutdown into one
  async process.
- Updated Docker deployment for host-local model access without publishing MCP or SearXNG ports.
- Added an explicit opt-in model smoke command that is excluded from automated tests.

## Verification

- Full suite after independent review fixes: `150 passed`.
- `ruff check src tests scripts`: passed.
- `ruff format --check src tests scripts`: passed.
- `docker compose config --quiet`: passed.
- `uv build --offline`: wheel and source distribution built.
- `docker build -t conference-scout:agent-test .`: image built successfully.
- Secret scan: no credential value found; matches were only the scan patterns in plan/tests.
- Real cloud/local model smoke: intentionally not run; it requires operator approval and may consume
  credits.

Independent review additionally verified and hardened atomic pending-action claims on SQLite,
restart/cancellation recovery, single-active-conversation enforcement, bounded incremental
compaction and memory retrieval, and owner-scoped conference mutations/listing/reminders.

## Operator next steps

1. Copy `.env.example` to `.env` and configure Telegram plus at least one model profile.
2. Start with `docker compose up -d`.
3. Open the bot, run `/model`, choose a currently available model, then send ordinary text.
4. Optionally run `scripts/smoke_model.py` explicitly if spending a model request is acceptable.
5. Back up `/data/conference_scout.db` before upgrades.
