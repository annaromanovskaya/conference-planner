# HTML-only crawling and synchronized progress

- Source catalog URLs and inspection candidates with document extensions (PDF, DOC/DOCX, XLS, PPT, CSV, archives, images, media) are excluded from HTTP/Playwright page inspection.
- Previously enqueued pending/running document inspections for the currently processed job are marked SKIPPED with error_code NON_HTML_DOCUMENT (without deleting original candidates).
- Candidate supporting pages use the same file URL filter.
- The source crawler selects only CATALOG URL tasks; inspection tasks cannot be mistaken for catalogs.
- Status card and details report both derive counters from the same DiscoverySearchAudit and DiscoveryURLTask records. "Исследовано сайтов" is replaced by explicit separate counters for catalogs and inspected candidate pages; the pending, running, skipped, and failure counts now match.
- The report still shows the latest 12 journal entries, but the counters cover the entire job.

After copying source changes and rebuilding, start a new search to process existing file tasks within that job. For existing running job 15, patch application restart resumes queued work automatically; old running tasks lease recovery is subject to the existing 5-minute lease.

Deployment: copy src/ to server src/ and rebuild confplanner; no migrations required.
