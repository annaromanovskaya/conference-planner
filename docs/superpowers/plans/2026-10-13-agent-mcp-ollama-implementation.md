# ConfPlanner Conversational Agent, MCP, and Ollama Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the owner-only Telegram bot an always-on conversational agent that can use confirmed Conference Scout actions through an internal MCP server and switch between Ollama Cloud, local Ollama, and local OpenAI-compatible models.

**Architecture:** Keep one async application process and SQLite ownership domain. Add provider-neutral model gateways, persisted model and memory state, a loopback Streamable HTTP MCP server, and a bounded agent loop; all domain writes become expiring Telegram-confirmed pending actions. Existing deterministic discovery, validation, monitoring, reminders, SSRF controls, and Pydantic validation remain authoritative.

**Tech Stack:** Python 3.12, aiogram 3, httpx, Pydantic v2, SQLAlchemy/aiosqlite, Alembic, MCP Python SDK v2, Starlette/Uvicorn, APScheduler, pytest/pytest-asyncio, Ruff, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-10-13-agent-mcp-ollama-design.md`

## Global Constraints

- The bot is always an AI chat; there is no separate chat mode or “AI chat” button.
- The selected provider and model persist across restarts and can be changed in Telegram.
- Ollama Cloud offers only currently available models from the configured free-credit allowlist.
- Read operations may execute immediately; domain mutations require an explicit Telegram confirmation.
- API keys remain environment-only and never enter the database, prompts, MCP results, Telegram output, or logs.
- The MCP endpoint binds to container loopback and is never published by Docker.
- The agent has no shell, filesystem, raw SQL, arbitrary URL fetch, or unrestricted web tool.
- Agent turns are limited to six model steps, three tool calls per step, a total timeout, and bounded result/context sizes.
- Existing source verification, SSRF protection, Pydantic validation, scheduling, monitoring, reminders, and owner-only access remain intact.
- The relational database remains the source of truth; no graph or vector database is introduced.
- Existing databases must migrate without losing topics, conferences, deadlines, sources, subscriptions, reminders, or history.
- Tests never call a real paid/cloud model unless the operator explicitly runs the smoke command.
- Before every task commit, run `.venv/bin/pytest -q` and report/fix every failure; focused commands prove the new RED/GREEN cycle but never replace the full regression suite.

## Review Focus

- A configured API key, user memory, or full prompt must never appear in logs, Telegram errors, persisted tool results, or MCP responses; Task 1 and Task 9 add secret-redaction tests.
- A model that repeatedly requests tools must stop at the configured step/call/time limits and return a controlled Russian error; Task 7 adds loop-limit and timeout tests.
- A stale, foreign-owner, cancelled, or already-executed pending action must never mutate domain state; Task 5 adds all four rejection tests.
- An existing pre-agent SQLite database must upgrade in place and preserve every legacy table row; Task 2 adds a legacy-schema migration test.
- Model names that disappear, contain long callback-unsafe text, or do not support tool calling must not break selection or ordinary chat; Tasks 3 and 8 add stale-model, indexed-callback, and chat-only tests.

---

### Task 1: Provider-Neutral Model Gateway and Configuration

**Files:**
- Create: `src/conference_scout/ai/models.py`
- Create: `src/conference_scout/ai/gateway.py`
- Create: `src/conference_scout/ai/ollama.py`
- Create: `src/conference_scout/ai/openai_compatible.py`
- Create: `tests/unit/ai/test_ollama.py`
- Create: `tests/unit/ai/test_openai_compatible.py`
- Modify: `src/conference_scout/config.py`
- Modify: `tests/unit/test_config.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**
- Produces: `ChatMessage`, `ToolDefinition`, `ToolCall`, `ChatResponse`, `AvailableModel`, and `ModelGateway` in `conference_scout.ai`.
- Produces: `OllamaGateway(client, base_url, api_key)` and `OpenAICompatibleGateway(client, base_url, api_key)` with `list_models()` and `chat(model, messages, tools=())`.
- Produces: optional settings for three profiles plus exact agent/MCP limits used by later tasks.
- Consumes: the existing shared `httpx.AsyncClient` lifecycle.

- [ ] **Step 1: Add failing configuration and Ollama adapter tests**

  Test that settings load without OpenRouter variables, parse the six default cloud model names, reject an empty configured allowlist, and keep keys as `SecretStr`. Test that `OllamaGateway.list_models()` calls `<base>/api/tags`, that Cloud authentication uses `Authorization: Bearer`, and that `chat()` sends `stream: false`, normalized messages, and tool schemas. Assert malformed/missing `message.content` and HTTP failures become typed `ModelGatewayError` values without response bodies or keys in their text.

- [ ] **Step 2: Run the focused tests and verify RED**

  Run: `.venv/bin/pytest -q tests/unit/test_config.py tests/unit/ai/test_ollama.py`

  Expected: FAIL because the new settings and gateway types do not exist.

- [ ] **Step 3: Implement the model DTOs, protocol, Ollama gateway, and settings**

  Define immutable normalized DTOs. `ModelGateway.chat()` accepts `model: str`, `messages: Sequence[ChatMessage]`, and `tools: Sequence[ToolDefinition]`; it returns `ChatResponse`. Add these settings with their spec defaults: cloud/local/OpenAI URLs and keys, the six-name cloud allowlist, `agent_max_steps=6`, `agent_max_tool_calls_per_step=3`, `agent_timeout_seconds=90`, bounded context/result sizes, and loopback MCP host/port. Remove the requirement for `OPENROUTER_API_KEY` and `OPENROUTER_MODEL`.

- [ ] **Step 4: Add failing OpenAI-compatible adapter tests**

  Test `/v1/models`, `/v1/chat/completions`, bearer authentication only when a key is configured, assistant tool-call parsing, plain content parsing, and safe typed failures.

- [ ] **Step 5: Run the OpenAI adapter tests and verify RED**

  Run: `.venv/bin/pytest -q tests/unit/ai/test_openai_compatible.py`

  Expected: FAIL because `OpenAICompatibleGateway` is missing.

- [ ] **Step 6: Implement the OpenAI-compatible gateway and dependencies**

  Add `mcp>=2,<3`, `alembic>=1.16,<2`, and `uvicorn>=0.35,<1` to runtime dependencies and refresh `uv.lock`. Keep provider HTTP translation isolated from registry, memory, and Telegram code.

- [ ] **Step 7: Run focused and regression tests**

  Run: `.venv/bin/pytest -q tests/unit/ai/test_ollama.py tests/unit/ai/test_openai_compatible.py tests/unit/test_config.py tests/unit/ai/test_service.py`

  Expected: PASS.

- [ ] **Step 8: Commit**

  ```bash
  git add pyproject.toml uv.lock src/conference_scout/config.py src/conference_scout/ai tests/unit/ai tests/unit/test_config.py
  git commit -m "feat: add Ollama and local model gateways"
  ```

### Task 2: Additive Database Schema and Alembic Migration

**Files:**
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/script.py.mako`
- Create: `migrations/versions/20261013_0001_agent_infrastructure.py`
- Create: `src/conference_scout/db/migrations.py`
- Create: `src/conference_scout/db/repositories/agent_state.py`
- Create: `src/conference_scout/db/repositories/conversations.py`
- Create: `src/conference_scout/db/repositories/memories.py`
- Create: `src/conference_scout/db/repositories/pending_actions.py`
- Create: `tests/unit/db/test_agent_repositories.py`
- Create: `tests/integration/test_migrations.py`
- Modify: `src/conference_scout/db/models.py`
- Modify: `src/conference_scout/db/session.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Produces: ORM models `ModelSelection`, `ModelCapability`, `Conversation`, `ChatMessageRecord`, `UserMemory`, `PendingAction`, and `KnowledgeRelation`.
- Produces: focused repositories with owner-scoped CRUD and no model/provider secrets.
- Produces: `run_migrations(database_url: str) -> None`, safe for a fresh or current legacy SQLite database.
- Consumes: existing `User`, `Topic`, `Conference`, and SQLAlchemy session factory.

- [ ] **Step 1: Write failing repository model tests**

  Assert one active model selection per user; one active conversation per user; stable ordered chat messages with optional domain-reference JSON; scoped active memories; unique provider/model capability cache rows; pending-action status/expiry fields; and provenance-bearing knowledge relations. Include owner-isolation assertions on every repository query.

- [ ] **Step 2: Run repository tests and verify RED**

  Run: `.venv/bin/pytest -q tests/unit/db/test_agent_repositories.py`

  Expected: FAIL because the tables and repositories are absent.

- [ ] **Step 3: Implement additive ORM models and repositories**

  Use new tables only; do not add required columns to legacy tables. Use enums or constrained string values for conversation, memory, pending-action, and relation status/kind. Index owner, active/status, expiry, and relation endpoints. Repository methods require `user_id` explicitly and return no cross-owner records.

- [ ] **Step 4: Write the failing migration compatibility test**

  Build a temporary database with the current pre-agent tables and representative rows, run `run_migrations`, and assert all legacy rows remain plus every new table and Alembic revision. Also migrate an empty database and assert the complete schema exists.

- [ ] **Step 5: Run migration tests and verify RED**

  Run: `.venv/bin/pytest -q tests/integration/test_migrations.py`

  Expected: FAIL because Alembic configuration and migration runner are missing.

- [ ] **Step 6: Implement the migration baseline and startup runner**

  The baseline must inspect the database: create the complete schema for a fresh database, or create only missing agent tables for the known legacy schema. It must fail clearly for a partial/unknown legacy schema instead of dropping or rewriting tables. Replace test fixture initialization with the migration runner, while retaining `initialize_database` only if an existing test boundary still needs it.

- [ ] **Step 7: Run database regression tests**

  Run: `.venv/bin/pytest -q tests/unit/db tests/integration/test_migrations.py tests/integration/test_discovery.py tests/integration/test_monitoring.py`

  Expected: PASS with all legacy rows preserved.

- [ ] **Step 8: Commit**

  ```bash
  git add alembic.ini migrations src/conference_scout/db tests/conftest.py tests/unit/db tests/integration/test_migrations.py
  git commit -m "feat: add persistent agent state migrations"
  ```

### Task 3: Model Registry, Selection, and Active LLM Bridge

**Files:**
- Create: `src/conference_scout/ai/registry.py`
- Create: `src/conference_scout/ai/active_provider.py`
- Create: `tests/unit/ai/test_registry.py`
- Modify: `src/conference_scout/ai/service.py`
- Modify: `tests/unit/ai/test_service.py`

**Interfaces:**
- Produces: `ProviderProfile(id, display_name, gateway)`, `ActiveModel(profile_id, model_name, supports_tools)`, and `ModelRegistry`.
- Produces: `ModelRegistry.available_profiles()`, `list_models(profile_id)`, `select(user_id, profile_id, model_name)`, `get_active(user_id)`, and `refresh_capability(user_id)`.
- Produces: `ActiveLLMProvider.complete(system_prompt, user_prompt)` satisfying the existing `LLMProvider` protocol for `AIService`.
- Consumes: Task 1 gateways/settings and Task 2 agent-state repositories.

- [ ] **Step 1: Write failing registry tests**

  Assert only configured profiles appear; Cloud results are the intersection of `/api/tags` and the free allowlist in allowlist order; local results are not filtered; selection rejects unknown/stale names; selected values persist; missing selection produces a setup-required error; capability probing stores `True` for a valid tool call and `False` for a plain-chat-only response.

- [ ] **Step 2: Run registry tests and verify RED**

  Run: `.venv/bin/pytest -q tests/unit/ai/test_registry.py`

  Expected: FAIL because `ModelRegistry` does not exist.

- [ ] **Step 3: Implement registry and capability probing**

  Use a harmless tool definition whose result is never executed to test tool-call syntax. Bound the probe by the configured timeout. Never infer Cloud free status beyond allowlist intersection. Return explicit typed errors for missing profile, missing model, authentication, and unavailable endpoint.

- [ ] **Step 4: Write failing active-provider tests**

  Change `AIService` tests to provide an `ActiveLLMProvider` over a fake registry. Assert the selected model is used, JSON-only instructions remain present, invalid structured output still retries exactly twice, and no OpenRouter type is imported.

- [ ] **Step 5: Run the bridge tests and verify RED**

  Run: `.venv/bin/pytest -q tests/unit/ai/test_service.py`

  Expected: FAIL until the active bridge is implemented.

- [ ] **Step 6: Implement active provider bridge**

  `ActiveLLMProvider` resolves selection per call so Telegram changes take effect without restart. It calls the selected gateway without tools and returns assistant content to the existing Pydantic-validation path. Keep the old adapter file temporarily so intermediate commits remain import-compatible; Task 9 removes it after composition and error middleware no longer reference it.

- [ ] **Step 7: Run AI regression tests**

  Run: `.venv/bin/pytest -q tests/unit/ai`

  Expected: PASS.

- [ ] **Step 8: Commit**

  ```bash
  git add src/conference_scout/ai tests/unit/ai
  git commit -m "feat: persist and resolve active agent models"
  ```

### Task 4: Conversation Memory, User Memory, and SQL Knowledge Graph

**Files:**
- Create: `src/conference_scout/agent/__init__.py`
- Create: `src/conference_scout/agent/memory.py`
- Create: `src/conference_scout/agent/knowledge.py`
- Create: `src/conference_scout/schemas/agent.py`
- Create: `tests/unit/agent/test_memory.py`
- Create: `tests/unit/agent/test_knowledge.py`

**Interfaces:**
- Produces: `MemoryService.get_context(user_id, query) -> MemoryContext`, `append_message(...)`, `start_new_conversation(user_id)`, `compact_conversation(user_id, summarizer)`, `list_memories(user_id)`, and conflict detection for proposed memories.
- Produces: `KnowledgeGraphService.search(user_id, query, limit)`, `conference_context(user_id, conference_id)`, and `related_conferences(user_id, conference_id, limit)`.
- Produces: Pydantic models `MemoryProposal`, `MemoryContext`, `KnowledgeNode`, `KnowledgeEdge`, and `KnowledgeSubgraph`.
- Consumes: Task 2 repositories and existing topic/conference/deadline repositories.

- [ ] **Step 1: Write failing working-memory tests**

  Assert a new conversation is reused until `/new`; recent messages are returned in stable order within the configured budget; older messages are replaced by a stored summary after compaction; domain references survive compaction; and the summarizer receives only user/assistant-visible content, never secrets or hidden reasoning.

- [ ] **Step 2: Run memory tests and verify RED**

  Run: `.venv/bin/pytest -q tests/unit/agent/test_memory.py`

  Expected: FAIL because the memory service and schemas are absent.

- [ ] **Step 3: Implement conversation and user memory service**

  Store original memory text and validated structured fields (`kind`, `scope`, optional topic/conference link, rule data, provenance). Retrieval combines global memories with query-matched scoped memories. Detect equivalent and contradictory active rules without silently resolving them.

- [ ] **Step 4: Write failing graph tests**

  Seed topics, conferences, relevance links, deadlines, sources, subscriptions, deadline history, relations, and scoped memories. Assert returned nodes/edges include provenance and timestamps, exclude other owners' private state, and support normalized text search plus bounded one-hop related-conference traversal.

- [ ] **Step 5: Run graph tests and verify RED**

  Run: `.venv/bin/pytest -q tests/unit/agent/test_knowledge.py`

  Expected: FAIL because the graph projection service is absent.

- [ ] **Step 6: Implement SQL-backed graph projection**

  Project existing relational facts instead of duplicating them. Use `KnowledgeRelation` only for relations not already represented, such as `SAME_AS` and `RELATED_TO`. Full-text behavior may use normalized SQL substring/token matching at this dataset size; keep it behind the service interface.

- [ ] **Step 7: Run focused tests**

  Run: `.venv/bin/pytest -q tests/unit/agent/test_memory.py tests/unit/agent/test_knowledge.py`

  Expected: PASS.

- [ ] **Step 8: Commit**

  ```bash
  git add src/conference_scout/agent src/conference_scout/schemas/agent.py tests/unit/agent
  git commit -m "feat: add durable agent memory and knowledge graph"
  ```

### Task 5: Confirmed Pending Actions

**Files:**
- Create: `src/conference_scout/agent/actions.py`
- Create: `tests/unit/agent/test_actions.py`
- Modify: `src/conference_scout/schemas/agent.py`

**Interfaces:**
- Produces: discriminated Pydantic payloads for add-topic, add-source, track, archive, save-memory, forget-memory, and run-discovery actions.
- Produces: `ConfirmationService.propose(user_id, payload, summary) -> PendingActionView`, `confirm(user_id, action_id) -> ActionResult`, and `cancel(user_id, action_id) -> PendingActionView`.
- Consumes: Task 2 pending-action repository, Task 4 memory service, and existing domain repositories/services.

- [ ] **Step 1: Write failing action lifecycle tests**

  For each action payload, assert proposal validation and a Russian human-readable summary. Assert confirmation performs the deterministic command once. Assert foreign-owner, expired, cancelled, malformed, and already-executed actions do not call executors. Assert executor failure leaves a retryable failed status without reporting success.

- [ ] **Step 2: Run tests and verify RED**

  Run: `.venv/bin/pytest -q tests/unit/agent/test_actions.py`

  Expected: FAIL because action schemas and service are missing.

- [ ] **Step 3: Implement pending actions and executor registry**

  Deserialize payloads through their exact Pydantic discriminator before execution. Lock/re-read the row inside the transaction, transition atomically, and commit domain mutation plus executed status together where possible. Model output must never choose an executor outside the allowlist.

- [ ] **Step 4: Run focused and repository tests**

  Run: `.venv/bin/pytest -q tests/unit/agent/test_actions.py tests/unit/db/test_agent_repositories.py tests/unit/bot/test_services.py`

  Expected: PASS.

- [ ] **Step 5: Commit**

  ```bash
  git add src/conference_scout/agent/actions.py src/conference_scout/schemas/agent.py tests/unit/agent/test_actions.py
  git commit -m "feat: require confirmation for agent mutations"
  ```

### Task 6: Internal MCP Server and Tool Client

**Files:**
- Create: `src/conference_scout/mcp/__init__.py`
- Create: `src/conference_scout/mcp/server.py`
- Create: `src/conference_scout/mcp/client.py`
- Create: `src/conference_scout/mcp/tools.py`
- Create: `tests/unit/mcp/test_tools.py`
- Create: `tests/integration/test_mcp_server.py`

**Interfaces:**
- Produces: `build_mcp_server(services: MCPToolServices) -> MCPServer` with the exact read/proposal tools in the spec.
- Produces: `ToolClient.list_tools() -> Sequence[ToolDefinition]` and `call_tool(name, arguments) -> ToolResult`.
- Produces: production `StreamableHTTPToolClient(url)` and an in-process test client.
- Consumes: Tasks 3–5 services plus existing discovery/search/fetch services through explicit callables in `MCPToolServices`.

- [ ] **Step 1: Write failing tool contract tests**

  Assert the server exposes exactly the ten read-only and seven proposal tools from the spec, each with a closed JSON input schema and concise description. Assert there is no shell, filesystem, SQL, raw fetch, secret, or generic web tool. Test representative read results are bounded and provenance-bearing; proposal tools return pending-action IDs rather than performing mutations.

- [ ] **Step 2: Run tool tests and verify RED**

  Run: `.venv/bin/pytest -q tests/unit/mcp/test_tools.py`

  Expected: FAIL because the MCP package does not exist.

- [ ] **Step 3: Implement MCP tool definitions and handlers**

  Register typed async functions on MCP SDK v2. Capture the trusted single owner and service dependencies at server construction; do not accept `user_id` from tool arguments. Normalize all results through Pydantic response models and truncate oversized result sets before serialization.

- [ ] **Step 4: Write failing transport integration tests**

  Start the MCP application in-process, negotiate capabilities, list tools, call one read tool and one proposal tool, and assert invalid names/arguments return protocol errors without crashing the server.

- [ ] **Step 5: Run integration test and verify RED**

  Run: `.venv/bin/pytest -q tests/integration/test_mcp_server.py`

  Expected: FAIL until client and server transport wiring exist.

- [ ] **Step 6: Implement client normalization and Streamable HTTP app**

  Convert MCP tool schemas/results to the Task 1 DTOs used by gateways. Export an ASGI application bound later by the composition root. Keep production URL loopback-only and make client read timeout configurable within the total agent timeout.

- [ ] **Step 7: Run MCP tests**

  Run: `.venv/bin/pytest -q tests/unit/mcp tests/integration/test_mcp_server.py`

  Expected: PASS.

- [ ] **Step 8: Commit**

  ```bash
  git add src/conference_scout/mcp tests/unit/mcp tests/integration/test_mcp_server.py
  git commit -m "feat: expose conference tools through internal MCP"
  ```

### Task 7: Bounded Agent Runtime

**Files:**
- Create: `src/conference_scout/agent/runtime.py`
- Create: `src/conference_scout/agent/prompts.py`
- Create: `tests/unit/agent/test_runtime.py`
- Modify: `src/conference_scout/agent/__init__.py`

**Interfaces:**
- Produces: `AgentRuntime.respond(user_id: int, text: str) -> AgentReply`.
- Produces: `AgentReply(text: str, pending_actions: tuple[PendingActionView, ...], references: tuple[DomainReference, ...])`.
- Consumes: `ModelRegistry`, Task 1 gateway DTOs, Task 4 `MemoryService`, Task 6 `ToolClient`, and configured limits.

- [ ] **Step 1: Write failing no-tool and single-tool tests**

  Test a plain response stores user/assistant messages; a tool request calls MCP, passes the result back to the same model, stores stable domain references, and returns final text; a chat-only model answers without tools and receives a system note that tools are unavailable.

- [ ] **Step 2: Run focused tests and verify RED**

  Run: `.venv/bin/pytest -q tests/unit/agent/test_runtime.py -k 'plain or single_tool or chat_only'`

  Expected: FAIL because `AgentRuntime` is missing.

- [ ] **Step 3: Implement minimal agent loop**

  Assemble system policy, relevant memories, summary, recent messages, and MCP tool definitions. Validate every tool call against the advertised schema before executing it. Save only visible assistant text, normalized tool results/references, and operational metadata; never persist thinking content.

- [ ] **Step 4: Add failing safety-limit tests**

  Test six-step exhaustion, more than three calls in one step, unknown tools, schema-invalid arguments, a hanging model/tool timeout, oversized tool results, and provider/MCP failures. Each must produce a controlled typed result suitable for a concise Russian Telegram error and must not execute unapproved calls.

- [ ] **Step 5: Run safety tests and verify RED**

  Run: `.venv/bin/pytest -q tests/unit/agent/test_runtime.py -k 'limit or timeout or unknown or invalid or oversized or failure'`

  Expected: FAIL until all guards are implemented.

- [ ] **Step 6: Implement limits, error normalization, and post-turn compaction**

  Wrap the entire turn in the configured timeout. Count model steps and calls before execution. Execute parallel calls only when every requested tool is read-only. Invoke conversation compaction after a successful turn when the history budget is exceeded.

- [ ] **Step 7: Run agent tests**

  Run: `.venv/bin/pytest -q tests/unit/agent`

  Expected: PASS.

- [ ] **Step 8: Commit**

  ```bash
  git add src/conference_scout/agent tests/unit/agent
  git commit -m "feat: add bounded conversational agent runtime"
  ```

### Task 8: Telegram-First Conversational UX

**Files:**
- Create: `src/conference_scout/bot/agent_router.py`
- Create: `src/conference_scout/bot/model_router.py`
- Create: `src/conference_scout/bot/confirmation_router.py`
- Create: `tests/unit/bot/test_agent_router.py`
- Create: `tests/unit/bot/test_model_router.py`
- Create: `tests/unit/bot/test_confirmation_router.py`
- Modify: `src/conference_scout/bot/router.py`
- Modify: `src/conference_scout/bot/keyboards.py`
- Modify: `src/conference_scout/bot/states.py`
- Modify: `src/conference_scout/bot/errors.py`
- Modify: `src/conference_scout/bot/dependencies.py`
- Modify: `tests/unit/bot/test_router.py`
- Modify: `tests/unit/bot/test_errors.py`

**Interfaces:**
- Produces: `/model`, `/new`, `/memory`, `/forget`, and `/help` handlers; a final owner-only free-text handler; model-selection and action-confirmation callbacks.
- Consumes: `AgentRuntime`, `ModelRegistry`, `MemoryService`, and `ConfirmationService` injected through dispatcher/middleware dependencies.

- [ ] **Step 1: Write failing conversational-routing tests**

  Assert `/start` explains direct conversation and does not require an AI-chat button; arbitrary text calls `AgentRuntime.respond`; `/new` starts a new conversation; `/memory` lists sanitized active memories; long agent replies split below Telegram limits; pending actions render confirmation buttons; and the free-text handler is registered after specific FSM/command handlers.

- [ ] **Step 2: Run routing tests and verify RED**

  Run: `.venv/bin/pytest -q tests/unit/bot/test_agent_router.py tests/unit/bot/test_router.py`

  Expected: FAIL because the conversational router is absent.

- [ ] **Step 3: Implement conversational handlers and message splitting**

  Preserve owner middleware as the first boundary. Emit chat action `typing` during agent work. Keep existing menu shortcuts optional, but route ordinary text directly to the agent without entering an FSM chat state.

- [ ] **Step 4: Write failing model-selection tests**

  Assert `/model` lists only configured profiles; model buttons use indexed callback data below 64 bytes while model names stay in FSM data; stale selections are rejected and refreshed; chat-only capability is labeled; no key or base URL appears in output.

- [ ] **Step 5: Implement model selection and diagnostics**

  Use FSM only for the short selection transaction, not for chat mode. Persist through `ModelRegistry.select` and return clear Russian errors for auth, unavailable endpoint, missing model, timeout, and rate/credit limit.

- [ ] **Step 6: Write failing confirmation tests**

  Assert confirm/cancel callbacks are owner-bound, expired actions show a harmless message, duplicate callback delivery cannot execute twice, and long/malformed IDs are rejected before repository access.

- [ ] **Step 7: Implement confirmation callbacks and generalized errors**

  Replace OpenRouter-specific middleware text with provider-neutral typed error mapping. Never include upstream bodies, prompts, keys, or memories in logs or Telegram responses.

- [ ] **Step 8: Run all bot tests**

  Run: `.venv/bin/pytest -q tests/unit/bot`

  Expected: PASS.

- [ ] **Step 9: Commit**

  ```bash
  git add src/conference_scout/bot tests/unit/bot
  git commit -m "feat: make Telegram bot a conversational agent"
  ```

### Task 9: Composition Root, MCP Lifecycle, and Secure Deployment

**Files:**
- Create: `src/conference_scout/mcp/lifecycle.py`
- Create: `tests/integration/test_agent_application.py`
- Modify: `src/conference_scout/app.py`
- Modify: `src/conference_scout/main.py`
- Modify: `src/conference_scout/healthcheck.py`
- Delete: `src/conference_scout/ai/openrouter.py`
- Modify: `compose.yaml`
- Modify: `Dockerfile`
- Modify: `.env.example`
- Modify: `tests/unit/test_deployment_files.py`
- Modify: `tests/unit/test_healthcheck.py`

**Interfaces:**
- Produces: one coordinated lifecycle for migrations, database, MCP loopback server/client, model gateways, Telegram polling, scheduler, and shutdown.
- Consumes: all prior task interfaces.

- [ ] **Step 1: Write failing application composition test**

  With fake Telegram, HTTP/model gateways, scheduler, and in-process MCP transport, assert startup runs migrations before repositories, constructs one shared session factory, serves tools, handles one agent message end-to-end, and shuts down MCP, bot, HTTP client, scheduler, and engine even after cancellation.

- [ ] **Step 2: Run integration test and verify RED**

  Run: `.venv/bin/pytest -q tests/integration/test_agent_application.py`

  Expected: FAIL because the new lifecycle is not wired.

- [ ] **Step 3: Refactor composition root and start loopback MCP**

  Build configured gateways and registry once, inject services explicitly, start Uvicorn on configured `127.0.0.1` port, wait for readiness before agent polling, and perform graceful shutdown in reverse order. Run Alembic before the async application begins. Remove OpenRouter construction, settings access, and the obsolete adapter module.

- [ ] **Step 4: Write failing deployment and secret-redaction tests**

  Assert Compose publishes no MCP or SearXNG ports, maps `host.docker.internal:host-gateway`, retains hardening, and accepts the new environment variables. Assert `.env.example` contains placeholders only and no real key pattern. Assert healthcheck does not call a model or consume credits.

- [ ] **Step 5: Implement deployment changes**

  Keep the two-service topology (`conference-scout`, `searxng`); do not add a bundled model container. Ensure `/data` remains writable under the non-root user and migrations can create/update the database. Preserve the read-only root filesystem and dropped capabilities.

- [ ] **Step 6: Run application/deployment regression tests**

  Run: `.venv/bin/pytest -q tests/integration/test_agent_application.py tests/unit/test_deployment_files.py tests/unit/test_healthcheck.py`

  Expected: PASS.

- [ ] **Step 7: Commit**

  ```bash
  git add src/conference_scout/app.py src/conference_scout/main.py src/conference_scout/healthcheck.py src/conference_scout/mcp/lifecycle.py compose.yaml Dockerfile .env.example tests/integration/test_agent_application.py tests/unit/test_deployment_files.py tests/unit/test_healthcheck.py
  git commit -m "feat: compose agent and MCP runtime"
  ```

### Task 10: Operator Documentation, Smoke Check, and Full Verification

**Files:**
- Create: `scripts/smoke_model.py`
- Create: `tests/unit/test_smoke_script.py`
- Modify: `README.md`
- Modify: `PROGRESS.md`
- Modify: `tests/unit/test_documentation.py`

**Interfaces:**
- Produces: explicit operator instructions for Cloud/local configuration, `/model`, direct conversational use, memory, confirmations, migration/backup, troubleshooting, and opt-in smoke verification.
- Produces: `scripts/smoke_model.py --profile PROFILE --model MODEL`, which performs one explicit chat/tool-capability check and prints no secrets.
- Consumes: public configuration/model gateway interfaces only.

- [ ] **Step 1: Write failing documentation and smoke-script tests**

  Assert README replaces OpenRouter setup with Ollama Cloud/local/OpenAI-compatible setup; documents that ordinary text always reaches the agent; explains `/model`, `/new`, `/memory`, confirmations, free allowlist behavior, host networking, migrations, backups, and smoke-test credit use. Test smoke argument validation and secret-safe failure output with a fake gateway.

- [ ] **Step 2: Run focused tests and verify RED**

  Run: `.venv/bin/pytest -q tests/unit/test_documentation.py tests/unit/test_smoke_script.py`

  Expected: FAIL because docs and smoke script are not updated.

- [ ] **Step 3: Implement documentation and opt-in smoke command**

  Include exact setup examples without real credentials. State that Cloud structured output is not provider-enforced and application validation/retries remain active. Update progress with actual verification results only.

- [ ] **Step 4: Run the complete automated suite and static checks**

  Run:

  ```bash
  .venv/bin/pytest -q
  .venv/bin/ruff check src tests scripts
  .venv/bin/ruff format --check src tests scripts
  docker compose config --quiet
  ```

  Expected: all tests pass, Ruff reports no issues, formatting is unchanged, and Compose configuration is valid. Report any pre-existing or environment-only failure by exact command and message.

- [ ] **Step 5: Build package and container**

  Run:

  ```bash
  UV_CACHE_DIR=/tmp/confplanner-uv-cache uv build --offline
  docker build -t conference-scout:agent-test .
  ```

  Expected: wheel/sdist and Docker image build successfully. If Docker daemon access is unavailable, request access and report that verification separately rather than claiming success.

- [ ] **Step 6: Inspect final diff and secret scan**

  Run:

  ```bash
  git diff --check
  git status --short
  rg -n '(sk-or-|OLLAMA_CLOUD_API_KEY=.{8,}|LOCAL_OPENAI_API_KEY=.{8,})' --glob '!uv.lock' --glob '!.env' .
  ```

  Expected: no whitespace errors, only intended files changed, no committed credential value. Preserve the user's existing `TASK.md` modification.

- [ ] **Step 7: Commit**

  ```bash
  git add README.md PROGRESS.md scripts/smoke_model.py tests/unit/test_documentation.py tests/unit/test_smoke_script.py
  git commit -m "docs: explain conversational agent deployment"
  ```

- [ ] **Step 8: Use the verification-before-completion skill**

  Re-run the full suite and cite the fresh command outputs in the handoff. Do not run the real model smoke command without the owner's explicit approval because it sends data to the configured endpoint and may consume credits.
