from datetime import date

from conference_scout.conferences.deduplication import CandidateIdentity, find_duplicate


def test_same_canonical_url_merges() -> None:
    existing = CandidateIdentity(1, "Astro Informatics", None, None, "https://event.org/")
    candidate = CandidateIdentity(None, "Other title", None, None, "https://EVENT.org/#top")
    assert find_duplicate(candidate, [existing]) == 1


def test_similar_name_and_same_date_merge() -> None:
    existing = CandidateIdentity(2, "Cool Stars 23", date(2027, 6, 1), "CS23", None)
    candidate = CandidateIdentity(None, "Cool Stars 23 Conference", date(2027, 6, 1), "CS23", None)
    assert find_duplicate(candidate, [existing]) == 2


def test_same_topic_does_not_merge_unrelated_events() -> None:
    existing = CandidateIdentity(3, "Machine Learning Astronomy", None, None, None)
    candidate = CandidateIdentity(None, "AI in Astrophysics", None, None, None)
    assert find_duplicate(candidate, [existing]) is None
