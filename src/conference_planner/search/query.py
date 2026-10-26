from collections.abc import Iterable, Sequence

EVENT_TYPES = ("conference", "workshop", "symposium", "meeting")


def generate_queries(
    keywords: Iterable[str],
    years: Sequence[int],
    trusted_domains: Iterable[str],
    *,
    max_queries: int = 200,
) -> list[str]:
    queries: dict[str, None] = {}
    clean_keywords = dict.fromkeys(item.strip() for item in keywords if item.strip())
    clean_domains = dict.fromkeys(item.strip().lower().strip("./") for item in trusted_domains)
    for keyword in clean_keywords:
        for year in years:
            for event_type in EVENT_TYPES:
                queries[f"{keyword} {year} {event_type}"] = None
            for domain in clean_domains:
                if domain:
                    queries[f"site:{domain} {keyword} {year}"] = None
    return list(queries)[:max_queries]
