"""Shared website access diagnostics and source provenance policy."""
from dataclasses import dataclass
from enum import StrEnum
import re
from urllib.parse import urlsplit


class AccessStatus(StrEnum):
    AVAILABLE = "available"
    NOT_MODIFIED = "not_modified"
    CAPTCHA_REQUIRED = "captcha_required"
    ACCESS_DENIED = "access_denied"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    EXTRACTION_FAILED = "extraction_failed"
    UNAVAILABLE = "unavailable"


class EvidenceOrigin(StrEnum):
    LIVE_PAGE = "live_page"
    VALIDATED_CACHE = "validated_cache"
    STALE_CACHE = "stale_cache"
    SEARCH_SNIPPET = "search_snippet"
    THIRD_PARTY_READER = "third_party_reader"


@dataclass(frozen=True, slots=True)
class PageDiagnostics:
    url: str
    status: AccessStatus
    evidence_origin: EvidenceOrigin | None = None
    http_status: int | None = None
    retry_after: str | None = None

    @property
    def verified_live(self) -> bool:
        return self.status in (AccessStatus.AVAILABLE, AccessStatus.NOT_MODIFIED) and self.evidence_origin in (EvidenceOrigin.LIVE_PAGE, EvidenceOrigin.VALIDATED_CACHE)


_CAPTCHA_MARKERS = (
    "g-recaptcha", "h-captcha", "hcaptcha", "cf-turnstile", "challenges.cloudflare.com",
    "captcha-container", "captcha_required", "verify you are human",
    "checking your browser before accessing", "please enable javascript and cookies to continue",
)


def looks_like_access_challenge(html: str) -> bool:
    """Conservative heuristic; avoid classifying normal conference CAPTCHA mentions."""
    sample = html[:100_000].casefold()
    if any(marker in sample for marker in _CAPTCHA_MARKERS):
        return True
    title = re.search(r"<title[^>]*>(.*?)</title>", sample, re.S)
    return bool(title and any(s in title.group(1) for s in (
        "just a moment", "attention required", "human verification", "access denied",
    )))


def safe_public_link(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    return url


def should_try_reader(error_code: str) -> bool:
    """Never delegate access-control and anti-bot failures to a third party."""
    return error_code in {"NOT_HTML", "EXTRACTION_FAILED"}
