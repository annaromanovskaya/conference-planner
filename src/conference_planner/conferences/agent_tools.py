"""Bounded, evidence-preserving discovery helpers exposed to the MCP agent."""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import urlsplit

from conference_planner.conferences.deduplication import CandidateIdentity, find_duplicate
from conference_planner.schemas.common import VerificationStatus


def official_site_queries(topic: str, year: int, domains: list[str], limit: int = 6) -> list[str]:
    """Target configured organizers; never infer official status from search position."""
    clean = " ".join(topic.split())[:180]
    if not clean or not 2000 <= year <= 2200:
        raise ValueError("valid topic and year required")
    queries = []
    for raw in domains:
        host = urlsplit(raw).hostname
        if not host or host in {"localhost", "127.0.0.1"}:
            continue
        q = f'site:{host} {clean} conference {year}'
        if q not in queries:
            queries.append(q)
        if len(queries) >= limit:
            break
    return queries


def compare_events(candidate: CandidateIdentity, entries: list[CandidateIdentity]) -> dict:
    """Preview duplicate candidates: automatic merge is deliberately not performed."""
    duplicate_id = find_duplicate(candidate, entries)
    return {"duplicate": duplicate_id is not None, "existing_id": duplicate_id,
            "action": "REVIEW_EXISTING" if duplicate_id is not None else "NEW_CANDIDATE",
            "automatic_merge": False}


def summarize_deadlines(research) -> dict:
    """Return evidence for each deadline without upgrading secondary claims."""
    claims = []
    for item in research.deadlines:
        status = item.verification_status
        confirmed = status in {VerificationStatus.OFFICIAL_CONFIRMED, VerificationStatus.ORGANIZER_CONFIRMED}
        claims.append({
            "type": item.deadline_type.value,
            "exact_date": item.exact_date.isoformat() if item.exact_date else None,
            "approximate_year": item.approximate_year,
            "approximate_month": item.approximate_month,
            "tentative": item.tentative,
            "verified": confirmed,
            "verification_status": status.value,
            "source_url": str(item.source_url),
            "evidence_excerpt": item.excerpt[:500],
        })
    return {"conference": research.name, "deadlines": claims,
            "confirmed_count": sum(x["verified"] for x in claims),
            "status": "NO_DEADLINES_FOUND" if not claims else "EVIDENCE_REPORTED"}


def refine_queries(topic: str, year: int, previous: list[str], failures: list[str] | None = None,
                   limit: int = 6) -> dict:
    """Deterministic query diversification; agent chooses which to execute next."""
    if not 2000 <= year <= 2200 or not topic.strip():
        raise ValueError("valid topic and year required")
    topic = " ".join(topic.split())[:160]
    candidates = [
        f'{topic} scientific conference {year}',
        f'{topic} international symposium {year} call for abstracts',
        f'{topic} astronomy workshop {year} registration',
        f'{topic} {year} meeting scientific society',
        f'{topic} {year} конференция тезисы',
        f'{topic} {year} conference official programme',
    ]
    previous_norm = {" ".join(p.casefold().split()) for p in previous}
    new = [q for q in candidates if " ".join(q.casefold().split()) not in previous_norm][:limit]
    failures = failures or []
    return {"queries": new, "requires_verification": True,
            "reason": "CHANGE_SEARCH_TERMS",
            "search_provider_errors": failures[:10],
            "note": "Search engine failure cannot be treated as no conferences existing."}
