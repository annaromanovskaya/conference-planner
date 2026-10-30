"""Filtering for HTML-only conference discovery and research."""
from urllib.parse import urlsplit, unquote
import re

_NON_HTML = re.compile(
    r"\.(?:pdf|docx?|dotx?|xlsx?|xlsm|csv|pptx?|ppsx?|odt|ods|odp|"
    r"rtf|tex|epub|zip|rar|7z|tar|gz|jpg|jpeg|png|gif|svg|webp|"
    r"mp3|mp4|mov|avi|exe|dmg|iso)(?:$)", re.I)

def is_document_url(url: str) -> bool:
    """Reject non-HTML URLs by their decoded URL path, ignoring query strings."""
    return bool(_NON_HTML.search(unquote(urlsplit(url).path).rstrip('/')))
