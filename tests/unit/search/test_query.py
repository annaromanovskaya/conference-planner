from conference_scout.search.query import generate_queries


def test_queries_cover_event_types_years_and_trusted_domain() -> None:
    queries = generate_queries(["astroinformatics"], [2026, 2027], ["iau.org"])
    assert "astroinformatics 2026 conference" in queries
    assert "astroinformatics 2027 workshop" in queries
    assert "site:iau.org astroinformatics 2027" in queries


def test_queries_are_deduplicated_and_bounded() -> None:
    queries = generate_queries([" stars ", "stars"], [2027], [], max_queries=2)
    assert len(queries) == 2
