# ConfPlanner Conversational Agent, MCP, and Ollama Design

**Date:** 2026-10-13

## Goal

Turn the existing owner-only Conference Scout Telegram bot into a conversational AI agent. Every ordinary Telegram message goes to the agent, which can answer directly or use a constrained set of conference tools through an internal MCP server. The owner can select Ollama Cloud, a local Ollama server, or a local OpenAI-compatible API without restarting the bot.

## Success Criteria

- The bot is always an AI chat; there is no separate chat mode or “AI chat” button.
- The selected provider and model persist across restarts and can be changed in Telegram.
- Ollama Cloud offers only currently available models from the configured free-credit allowlist.
- Local Ollama and OpenAI-compatible endpoints can list and use their configured models.
- A bounded agent loop can call MCP tools and incorporate their results into its reply.
- Read operations may execute immediately; domain mutations require an explicit Telegram confirmation.
- Conversation history, verified conference knowledge, and user-authored memories survive restarts.
- Existing source verification, SSRF protection, owner-only access, scheduling, monitoring, and reminders remain intact.

## Non-Goals

- Autonomous multi-agent research or unattended model-authored changes.
- Shell, filesystem, raw SQL, arbitrary URL fetching, or unrestricted web access for the model.
- A public MCP endpoint.
- A graph database or vector database in the first release.
- Persisting model chain-of-thought.
- Automatically discovering which Ollama models are covered by free credits; Ollama's model API does not expose that billing property.

## Architecture

The application remains one async Python process and one SQLite writer domain. In addition to aiogram polling and APScheduler, it runs an MCP Streamable HTTP application on container loopback. The MCP endpoint is not published by Docker. `AgentRuntime` connects to that endpoint as an MCP client, translates MCP tool definitions into model tool schemas, executes a bounded tool loop, and returns the final text to Telegram.

Keeping the MCP server in the same process avoids concurrent application processes writing the SQLite database while preserving a real MCP protocol boundary. Tool handlers call application services and repositories rather than reaching into Telegram handlers or issuing raw SQL.

The main components are:

- `ModelGateway`: provider-neutral chat and tool-calling protocol.
- `OllamaProvider`: native `/api/chat`, `/api/tags`, and model inspection for Ollama Cloud or local Ollama.
- `OpenAICompatibleProvider`: `/v1/chat/completions` and `/v1/models` for other local model servers.
- `ModelRegistry`: reads configured profiles, lists models, tracks capability checks, and resolves the selected profile/model.
- `AgentRuntime`: assembles context and runs the bounded model/tool loop.
- `MemoryService`: retrieves and stores conversation, preference, note, and graph context.
- `ConfirmationService`: creates and executes owner-bound, expiring, single-use pending actions.
- `ConferenceMCPServer`: exposes only the approved domain tools.
- Existing discovery, monitoring, reminder, fetch, search, validation, and repository services.

The existing `AIService` continues to validate all structured conference output with Pydantic and bounded retries. It obtains the active provider/model through the model registry, so the same selected model is used for orchestration and conference analysis by default. Interfaces leave room for separate role-specific models later.

## Model Profiles and Selection

Three optional environment-defined profiles are supported:

```env
OLLAMA_CLOUD_BASE_URL=https://ollama.com
OLLAMA_CLOUD_API_KEY=secret

OLLAMA_LOCAL_BASE_URL=http://host.docker.internal:11434
OLLAMA_LOCAL_API_KEY=

LOCAL_OPENAI_BASE_URL=http://host.docker.internal:8000/v1
LOCAL_OPENAI_API_KEY=
```

Secrets are never stored in the database, sent to Telegram, returned by MCP, or included in prompts. A profile is visible only when its required configuration is present. Docker Compose maps `host.docker.internal` through `host-gateway` for Linux hosts.

Ollama Cloud models are fetched from `https://ollama.com/api/tags` and intersected with this default configurable free-credit allowlist:

- `gemma4:31b`
- `gpt-oss:120b`
- `gpt-oss:20b`
- `nemotron-3-nano:30b`
- `nemotron-3-super`
- `nemotron-3-ultra`

The allowlist can be changed through an environment variable when Ollama changes its free-credit policy. Local profiles show models returned by their own model-list endpoint. Telegram saves only the profile identifier and model name.

Selecting a model runs a bounded connectivity and tool-calling capability check. A model that answers chat but cannot call tools remains selectable and is labeled “chat only.” Agent-capable models are labeled separately. Capability results are cached with a timestamp and may be refreshed.

Ollama Cloud currently does not enforce structured outputs. Existing prompt constraints, Pydantic validation, and bounded retries therefore remain mandatory instead of relying on provider-side JSON schemas.

## Conversational Telegram Interface

There is no separate chat state. After `/start`, every ordinary owner message is handled by `AgentRuntime`. Examples include:

- “Find stellar spectroscopy conferences in 2027.”
- “What are my nearest deadlines?”
- “Track the second conference.”
- “Remember that I do not want events in the USA.”
- “Tell me more about the first result.”
- General questions that do not require a tool.

Commands provide control rather than entering modes:

- `/model` opens provider and model selection.
- `/new` starts a new conversation while retaining long-term memories and conference knowledge.
- `/memory` lists active user memories and preferences.
- `/help` explains capabilities and confirmation rules.

Inline buttons are used for provider/model selection and for `Confirm`/`Cancel`. Existing reply-menu actions may remain as optional shortcuts, but they route through the same services and are not required to use the bot. Long replies are split safely at Telegram limits, and the bot emits a typing indicator while work is in progress.

References such as “the second one” are resolved from recent conversation messages and structured tool-result references, not by guessing from global database order.

## Agent Loop

For each incoming message, the runtime loads:

1. System policy and tool-use rules.
2. Active user preferences and relevant notes.
3. The current conversation summary.
4. Recent conversation messages within a configured context budget.
5. Tool definitions returned by MCP.

The selected model either returns final text or requests tools. The runtime validates every requested name and argument object against the MCP schema, executes allowed calls, appends normalized tool results, and asks the model again. A turn is limited to six model steps, three tool calls per step, a total timeout, and bounded tool-result sizes. Parallel calls are allowed only for read-only tools. Mutating proposals are serialized.

Unknown tools, invalid arguments, exhausted limits, or tool errors become explicit tool results so the model can explain the failure. The runtime never executes text emitted by the model as code.

## MCP Tools and Authority

Read-only tools execute immediately:

- `list_topics`
- `list_conferences`
- `get_conference_context`
- `list_upcoming_deadlines`
- `search_knowledge`
- `find_related_conferences`
- `list_memories`
- `get_active_model`
- `search_event_candidates`
- `inspect_event_candidate`

Mutating tools only create a pending action:

- `propose_add_topic`
- `propose_add_source`
- `propose_track_conference`
- `propose_archive_conference`
- `propose_save_memory`
- `propose_forget_memory`
- `propose_run_discovery`

A pending action contains the owner, action type, validated payload, human-readable summary, creation time, expiry, and status. Telegram displays the summary with confirmation buttons. Only the owner can confirm it, and confirmation executes a deterministic application command exactly once. The model cannot invoke confirmation.

MCP does not expose shell execution, filesystem access, API secrets, raw SQL, unrestricted `fetch_url`, or a general-purpose web tool. Conference web research remains behind the existing SearXNG, URL-safety, bounded-fetch, untrusted-content, extraction, and provenance pipeline.

## Memory and Knowledge Graph

### Working Conversation Memory

Conversations and messages are persisted. Prompt assembly uses a configured recent-message budget plus a compact conversation summary. `/new` closes the active conversation and creates a new one. `/forget` removes chat history only after confirmation; it does not delete conference data or long-term memories.

Tool results stored in conversation history keep stable references to returned domain objects. This lets follow-up messages resolve “the first conference” without copying entire tool payloads into every prompt.

### User Memory

The agent may propose arbitrary notes, preferences, and instructions, including:

- “This conference is high priority.”
- “Do not recommend events in the USA.”
- “Prefer workshops with remote participation.”

Each memory stores the original text, a validated structured representation, scope (`global`, `topic`, or `conference`), optional linked object, creation/update timestamps, active status, and provenance from the confirming Telegram message. Saving, replacing, deactivating, or deleting memory requires confirmation.

Conflicting active rules are not silently overwritten. The confirmation screen shows the existing and proposed rules and offers replacement or coexistence. User preferences influence ranking and presentation but never rewrite verified conference facts.

### Verified Domain Knowledge

The relational database remains the source of truth. `KnowledgeGraphService` exposes graph-shaped traversal over existing and new relations:

- conference `RELEVANT_TO` topic, with score;
- conference `HAS_DEADLINE` deadline;
- deadline `VERIFIED_BY` source URL;
- owner `TRACKS` conference;
- conference `SAME_AS` or `RELATED_TO` conference;
- deadline `CHANGED_FROM` a historical date;
- memory `APPLIES_TO` a global, topic, or conference scope.

Every factual edge has provenance and timestamps. Model suggestions do not become verified nodes or edges until deterministic validation succeeds and, for owner-authored changes, the owner confirms them.

The first release uses SQL relationships and full-text/normalized search. A graph or vector database is deferred until dataset size or retrieval quality demonstrates a need. Embeddings may later become a replaceable secondary index; they will not become the source of truth.

## Persistence and Migrations

New tables cover model selection/capabilities, conversations/messages, user memories, pending actions, and any graph relations not already represented by existing tables. API secrets remain environment-only.

Alembic is introduced with a baseline that supports both a fresh database and the current deployed schema. Startup applies migrations before polling begins. Migrations are additive for this feature and must preserve existing topics, conferences, deadlines, sources, subscriptions, reminders, and history.

## Security and Failure Handling

- Existing owner-only middleware remains the first Telegram boundary.
- The MCP server binds only to container loopback and has no published port.
- Tool handlers receive owner context from the trusted application, not from model-supplied IDs.
- Pending actions are owner-bound, expiring, schema-validated, and single-use.
- Provider errors distinguish authentication, unavailable endpoint, missing model, unsupported tools, rate/credit limits, timeout, malformed response, and generic upstream failure.
- User-facing errors are concise Russian messages with a retry or configuration hint.
- Logs contain provider/profile name, model name, timings, step counts, and tool names, but no secrets, full prompts, page bodies, or private memories.
- External page text remains explicitly marked untrusted and cannot redefine system or tool policies.
- Context, response, page, tool-result, and loop sizes are bounded.

## Testing

Unit tests cover:

- Ollama and OpenAI-compatible request/response translation;
- cloud allowlist intersection and local model listing;
- persisted model selection and capability status;
- successful, invalid, looping, timed-out, and tool-free agent turns;
- tool schema validation and authority classification;
- pending-action ownership, expiry, cancellation, and exactly-once execution;
- conversation summarization and context budgeting;
- memory creation, conflicts, retrieval, scoping, and deletion;
- graph traversal and provenance;
- Telegram commands, free-text routing, model selection, and confirmation callbacks;
- additive migration from the current schema.

Integration tests use fake model endpoints and an in-process MCP server, require no secrets, and verify complete Telegram-to-tool-to-response flows. Existing discovery, monitoring, reminder, security, and repository suites remain green. A manual opt-in smoke command checks a real local or cloud model without running during the normal test suite or consuming credits unexpectedly.

## Deployment and Operator Workflow

The default Compose deployment remains `conference-scout` plus internal SearXNG. No local model container is bundled. Operators point the configured profile at an existing local model endpoint or Ollama Cloud. Health checks cover the database and application lifecycle without spending model credits; provider checks are exposed through `/model` diagnostics.

After configuration, the owner starts the stack, sends `/start`, chooses `/model`, and then writes ordinary messages directly to the bot. No chat-mode activation is required.

## References

- Ollama Cloud API and authentication: <https://docs.ollama.com/cloud>
- Ollama tool calling and agent loops: <https://docs.ollama.com/capabilities/tool-calling>
- Ollama structured-output limitation in Cloud: <https://docs.ollama.com/capabilities/structured-outputs>
- MCP Python SDK Streamable HTTP deployment: <https://py.sdk.modelcontextprotocol.io/run/>
