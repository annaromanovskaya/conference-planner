# Verified Conference Discovery and Monitoring Design

## Purpose

ConfPlanner must turn conversational research into source-backed database records. The owner asks
the Telegram bot to find or inspect conferences; the model may decide which research tools to use,
but it may claim data was saved only after a deterministic persistence service commits the change
and reads it back.

New conferences remain owner-controlled: search produces candidate cards with Add, Details, and
Skip actions. Once a conference is added to tracking, later fact and deadline updates are automatic
and require no additional confirmation.

## Problems Being Solved

The current model can say that dates were updated without invoking a write tool. Minimal fallback
imports contain no deadlines, while the monitor only modifies deadline rows that already exist.
Consequently it cannot bootstrap an unverified tracked conference. Monitoring failures also remain
in transient reports and are invisible to the owner.

## Authority Policy

- Research, extraction, and verification are read-only and need no confirmation.
- A newly discovered conference becomes tracked only after the owner presses `Добавить`.
- Tracked conference facts and deadlines may then be added or updated automatically.
- Archive, delete, and forget operations retain the existing confirmation rules.
- Models receive no SQL access and cannot bypass typed application services.

## Agent Architecture

The orchestrator interprets owner intent and selects typed MCP tools for candidate search, URL
inspection, verification, persisted lists, or immediate monitoring. It does not decide whether a
write succeeded.

The research agent searches evidence pages in this order: official conference site, official
organizer, then reputable secondary sources if official pages are unavailable. Every extracted
claim contains a normalized value, source URL and class, bounded page excerpt, observation time,
and tentative wording where applicable. Search snippets locate pages but are not evidence alone.

The verification agent independently compares claims with the supplied page evidence and may not
use outside knowledge. It assigns `OFFICIAL_CONFIRMED`, `ORGANIZER_CONFIRMED`,
`SECONDARY_UNVERIFIED`, `TENTATIVE`, `CONFLICT`, or `UNVERIFIABLE`. Lower-precedence evidence
cannot silently replace a conflicting official fact.

The deterministic persistence service schema-validates verified claims, deduplicates records, and
writes conference facts, deadlines, sources, history, and attempt state in one transaction. It then
reads affected rows back and returns a persistence receipt with IDs and created/updated fields. Only
that receipt permits the bot to say `сохранено` or `обновлено`.

## Data Model

Conference data includes official and alternative names, canonical website, source URL, exact event
start/end dates, location, description, verification state, and last successful check time.
Deduplication uses canonical URL, normalized name, and aliases.

A deadline supports either an exact date or an approximate period containing year and optional
month. A month-only deadline is stored and displayed as `Январь 2027 — ориентировочно`; the system
never invents a first or last day. Each deadline also stores type, source metadata, bounded evidence,
tentative flag, verification state, and observation time. Deadline types include registration
opening, oral abstract submission, acceptance notification, poster submission, registration, and
payment in addition to existing types.

Every material change stores old and new values, source, verification state, and detection time.
Every monitoring attempt stores success or a sanitized failure classification. Secrets, upstream
response bodies, and unbounded page content are never stored.

## Candidate Flow

1. The owner asks naturally or presses `Найти конференции`.
2. The orchestrator invokes research with owner topics, memory rules, query, and years.
3. Candidates are deduplicated, ranked, and evidence-checked without becoming tracked.
4. Telegram renders candidate cards with `Добавить`, `Подробнее`, and `Пропустить`.
5. `Добавить` invokes deterministic import and tracking followed by database read-back.
6. The success message lists only persisted facts and visibly labels unverified fields.

Candidate callbacks use durable database state rather than process-local IDs that disappear after a
restart.

## Monitoring Flow

Monitoring runs daily at `MONITORING_HOUR` in `APP_TIMEZONE`; a tracked conference card also offers
`Проверить сейчас`.

For every tracked conference the service:

1. loads existing facts, aliases, sources, deadlines, and attempt history;
2. fetches the official site and saved deadline pages;
3. falls back to organizer and reputable secondary sources when official pages are unavailable;
4. supplies the tracked name as extraction target so catalog pages can be disambiguated;
5. verifies claims and detects conflicts;
6. creates missing facts and deadlines as well as updating existing ones by source precedence;
7. writes all changes atomically and reads them back;
8. notifies the owner about new deadlines, changed facts, conflicts, or prolonged failures, while a
   successful no-change run stays silent.

TLS verification is never disabled. A broken certificate triggers fallback research. Monitoring
must bootstrap a placeholder conference; it cannot be limited to pre-existing deadline rows.

## Telegram Interface

Candidate cards show title, relevance, known dates, primary source, and verification status.
`Конференции` provides navigable `Отслеживаемые`, `Кандидаты`, and `Архив` lists. A persisted
conference card shows event dates, location, official site, deadlines, sources, verification state,
and last successful check, with `Проверить сейчас`, `История изменений`, and `В архив` actions.

`Ближайшие дедлайны` reads only database state and shows exact future dates followed by approximate
periods. Details expose source and verification status. Failures identify their stage instead of
showing only `Действие недоступно`.

## IMBASE 2027 Acceptance Example

Using `https://meetings.iac.es/imbase2027/pages/important-dates.php` or permitted fallback evidence,
the persisted result represents:

- November 2026: registration and abstract submission opens, tentative;
- January 2027: oral-contribution abstract submission closes, tentative;
- March 2027: oral-contribution acceptance notification, tentative;
- May 2027: registration, poster contribution, and payment deadline, tentative;
- 12–16 July 2027: exact event dates;
- Tenerife, Canary Islands, Spain: location.

The conference card and deadline list must display the same committed facts and sources.

## Reliability and Security

- URL safety and SSRF protection apply to every URL and redirect.
- Retries are bounded and distinguish network, extraction, verification, and persistence failures.
- Model output is schema-validated and cannot choose arbitrary write operations.
- Failed transactions leave no partial update; repeated imports and callbacks are idempotent.
- Logs contain sanitized stage and record identifiers, never credentials or authenticated bodies.
- Repeated monitoring failures become visible in conference details and eventually notify the owner.

## Testing and Completion

Tests cover exact and approximate deadlines; source precedence and conflicts; TLS and official-site
failure; multi-event catalog pages; creation of initially missing deadlines; history; duplicate
imports; rollback and persistence receipts; prohibition of unsupported update claims; durable
candidate callbacks; Telegram lists and actions; and end-to-end IMBASE 2027 persistence and display.

The work is complete when ordinary chat produces candidate cards, owner-approved candidates become
tracked, monitoring automatically populates and updates source-backed exact or approximate
deadlines, Telegram lists mirror database state, and every save/update claim has a persistence
receipt. Official-source failure may use labeled fallback evidence but may never cause invented
dates or insecure TLS.
