# Deep conference research (2026-10-30)

Changes:
- All enabled user sources are used; no 12-source limit.
- Direct source crawling supports depth 4 and up to 200 retrieved catalog pages, with no per-page candidate count or global 60-candidate cutoff. All accepted links persist in SQLite.
- Inspection queue contains all active, not-yet-researched candidates. There is no fixed 12-candidate cap; inspection runs until its time budget is exhausted. Remaining candidates remain in SQLite and are picked up on the next run.
- Within each candidate, linked programme, topics, sessions, abstracts, talks, posters and related pages are prioritised. Up to 18 linked evidence documents can be gathered, and the existing LLM relevance assessor receives combined extracted evidence.
- Existing LLM verification, evidence validation, topic matching and official-source checks remain intact. A verified page is not necessarily a confirmed relevant 2027 conference.

Operational controls (optional environment variables in Compose service):
- `DISCOVERY_CRAWL_DEPTH=4`
- `DISCOVERY_CRAWL_MAX_PAGES=200`
- `DISCOVERY_INSPECTION_SECONDS=1200`

Safety budgets still apply: workflow timeout 1800 seconds; crawler limits retrieved pages to avoid infinite traversal. The bot must be able to resume across runs to cover a very large candidate set. HTML pages are supported; PDF/Word are still skipped by the catalog crawler. Javascript-rendered programmes and PDF-only programmes are not extracted by this implementation. Deep semantic relevance relies on the configured LLM being available.

Validation: Python compileall passes. Unit tests cannot run in the current sandbox because `mcp` and `apscheduler` are not installed.
