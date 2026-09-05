from datetime import date

import pytest
from pydantic import ValidationError

from conference_scout.schemas.common import DeadlineType
from conference_scout.schemas.conferences import ConferenceExtraction, ExtractedDeadline
from conference_scout.schemas.topics import RelevanceAssessment, TopicNormalization


def test_topic_normalization_requires_search_keywords() -> None:
    with pytest.raises(ValidationError):
        TopicNormalization(name="Astronomy", description="Stars", keywords=[])


def test_relevance_score_is_bounded() -> None:
    with pytest.raises(ValidationError):
        RelevanceAssessment(score=1.01, explanation="Relevant")


def test_extracted_deadline_requires_source_url() -> None:
    with pytest.raises(ValidationError):
        ExtractedDeadline(
            deadline_type=DeadlineType.ABSTRACT_SUBMISSION,
            deadline_date=date(2027, 2, 12),
            source_url=None,
        )


def test_conference_extraction_accepts_source_backed_deadline() -> None:
    extraction = ConferenceExtraction(
        name="Astroinformatics 2027",
        source_url="https://example.org/event",
        deadlines=[
            ExtractedDeadline(
                deadline_type=DeadlineType.ABSTRACT_SUBMISSION,
                deadline_date=date(2027, 2, 12),
                source_url="https://example.org/dates",
            )
        ],
    )

    assert extraction.deadlines[0].deadline_date == date(2027, 2, 12)
