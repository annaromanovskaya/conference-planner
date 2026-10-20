import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.conferences.cycle import (
    ConferenceCycleService,
    CycleMode,
    CycleRequest,
)
from conference_scout.conferences.research import CandidateSearchReport
from conference_scout.db.repositories.agent_workflows import AgentWorkflowRepository
from conference_scout.db.repositories.users import UserRepository
from conference_scout.runtime_clock import ApplicationClock
from conference_scout.schemas.common import (
    AgentRunStatus,
    CandidateDecisionReason,
    CandidateStatus,
)


class CandidateRepository:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.skipped = SimpleNamespace(
            id=21,
            status=CandidateStatus.SKIPPED,
            decision_reason=CandidateDecisionReason.USER_DEFERRED,
        )

    async def list_candidates_by_statuses(self, _user_id, statuses, *, limit=100):
        self.order.append("decisions")
        if CandidateStatus.SKIPPED in statuses:
            return [self.skipped]
        return []


class Monitoring:
    def __init__(self, order: list[str], *, fail: bool = False, gate=None) -> None:
        self.order = order
        self.fail = fail
        self.gate = gate
        self.calls = 0

    async def run(self, _user_id, *, force=False):
        self.calls += 1
        self.order.append("tracked")
        assert force is True
        if self.gate is not None:
            self.gate.set()
            await asyncio.sleep(0.05)
        if self.fail:
            raise RuntimeError("monitoring unavailable")
        return SimpleNamespace(
            created=[],
            changed=[SimpleNamespace(conference_id=1)],
            conflicts=[],
            unchanged=[],
            changes=[],
            errors=[],
            catalogs=[],
        )


class Research:
    def __init__(self, order: list[str], *, new_ids=(31,)) -> None:
        self.order = order
        self.repository = CandidateRepository(order)
        self.new_ids = new_ids

    async def recheck_skipped(self, _user_id, candidate_id):
        self.order.append(f"recheck:{candidate_id}")
        return True

    async def search_fresh(
        self, _user_id, query, *, page_limit, results_per_page
    ):
        self.order.append("search")
        assert query == "stellar astronomy 2027"
        assert page_limit == 3
        assert results_per_page == 20
        return CandidateSearchReport(
            active_ids=self.new_ids,
            new_ids=self.new_ids,
            pages_checked=2,
            duplicates=4,
            exhausted=not self.new_ids,
        )


def clock() -> ApplicationClock:
    return ApplicationClock.start(
        ZoneInfo("Europe/Moscow"),
        now_provider=lambda: datetime(2026, 10, 20, 9, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_cycle_runs_stages_in_order_and_keeps_partial_results(
    session: AsyncSession,
) -> None:
    user = await UserRepository(session).get_or_create_by_telegram_id(8_001)
    order: list[str] = []
    service = ConferenceCycleService(
        AgentWorkflowRepository(session),
        Monitoring(order, fail=True),
        Research(order),
        application_clock=clock(),
    )

    report = await service.run(
        user.id,
        CycleRequest(
            mode=CycleMode.MANUAL,
            query="stellar astronomy 2027",
            page_limit=3,
            results_per_page=20,
        ),
    )

    assert order == ["tracked", "decisions", "recheck:21", "search"]
    assert report.status == AgentRunStatus.COMPLETED
    assert report.reactivated_candidates == 1
    assert report.new_candidate_ids == (31,)
    assert report.duplicates == 4
    assert report.errors == ("TRACKED_CHECK_FAILED",)


@pytest.mark.asyncio
async def test_cycle_with_no_new_candidates_completes_successfully(
    session: AsyncSession,
) -> None:
    user = await UserRepository(session).get_or_create_by_telegram_id(8_002)
    order: list[str] = []
    service = ConferenceCycleService(
        AgentWorkflowRepository(session),
        Monitoring(order),
        Research(order, new_ids=()),
        application_clock=clock(),
    )

    report = await service.run(
        user.id,
        CycleRequest(
            mode=CycleMode.MANUAL,
            query="stellar astronomy 2027",
            page_limit=3,
            results_per_page=20,
        ),
    )

    assert report.status == AgentRunStatus.COMPLETED
    assert report.new_candidate_ids == ()
    assert report.errors == ()


@pytest.mark.asyncio
async def test_cycle_is_single_flight_per_owner_but_independent_between_owners(
    session: AsyncSession,
) -> None:
    first = await UserRepository(session).get_or_create_by_telegram_id(8_003)
    second = await UserRepository(session).get_or_create_by_telegram_id(8_004)
    gate = asyncio.Event()
    order: list[str] = []
    monitoring = Monitoring(order, gate=gate)
    service = ConferenceCycleService(
        AgentWorkflowRepository(session),
        monitoring,
        Research(order),
        application_clock=clock(),
    )
    request = CycleRequest(
        mode=CycleMode.MANUAL,
        query="stellar astronomy 2027",
        page_limit=3,
        results_per_page=20,
    )

    first_call = asyncio.create_task(service.run(first.id, request))
    await gate.wait()
    duplicate_call = asyncio.create_task(service.run(first.id, request))
    first_report, duplicate_report = await asyncio.gather(
        first_call, duplicate_call
    )
    second_report = await service.run(second.id, request)

    assert first_report.run_id == duplicate_report.run_id
    assert second_report.run_id != first_report.run_id
    assert monitoring.calls == 2
