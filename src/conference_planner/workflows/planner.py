import re

from conference_planner.schemas.common import AgentTaskType
from conference_planner.schemas.workflows import WorkflowPlan, WorkflowTaskSpec


def is_conference_research_request(text: str) -> bool:
    value = text.casefold()
    asks_to_search = bool(
        re.search(
            r"\b(поищ(?:и|ите)|ищ(?:и|ите)|найд(?:и|ите)|поиск|проверь(?:те)?)\b",
            value,
        )
    )
    conference_context = bool(
        re.search(
            r"\b(конференц|мероприят|дедлайн|call for papers|воркшоп)\w*",
            value,
        )
    )
    return asks_to_search and conference_context


class WorkflowPlanner:
    _URL = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)

    async def plan(self, user_id: int, request_text: str) -> WorkflowPlan:
        del user_id
        url_match = self._URL.search(request_text)
        if not is_conference_research_request(request_text):
            return WorkflowPlan(
                tasks=(
                    WorkflowTaskSpec(
                        task_id="read", task_type=AgentTaskType.READ_DATABASE
                    ),
                    WorkflowTaskSpec(
                        task_id="answer",
                        task_type=AgentTaskType.SYNTHESIZE_RESPONSE,
                        dependencies=("read",),
                    ),
                )
            )
        tasks: list[WorkflowTaskSpec] = []
        dependency: tuple[str, ...] = ()
        if url_match is None:
            tasks.append(
                WorkflowTaskSpec(
                    task_id="search",
                    task_type=AgentTaskType.SEARCH_CANDIDATES,
                    input_data={"query": request_text},
                )
            )
            dependency = ("search",)
        tasks.append(
            WorkflowTaskSpec(
                task_id="crawl",
                task_type=AgentTaskType.CRAWL_CANDIDATE,
                dependencies=dependency,
                input_data={"url": url_match.group(0).rstrip(".,)")} if url_match else {},
            )
        )
        tasks.extend(
            (
                WorkflowTaskSpec(
                    task_id="extract",
                    task_type=AgentTaskType.EXTRACT_RESEARCH,
                    dependencies=("crawl",),
                ),
                WorkflowTaskSpec(
                    task_id="verify",
                    task_type=AgentTaskType.VERIFY_RESEARCH,
                    dependencies=("extract",),
                ),
                WorkflowTaskSpec(
                    task_id="propose",
                    task_type=AgentTaskType.PROPOSE_PERSISTENCE,
                    dependencies=("verify",),
                ),
            )
        )
        return WorkflowPlan(tasks=tuple(tasks))

    @staticmethod
    def conference_cycle_plan(query: str) -> WorkflowPlan:
        return WorkflowPlan(
            tasks=(
                WorkflowTaskSpec(
                    task_id="check_tracked",
                    task_type=AgentTaskType.CHECK_TRACKED_CONFERENCES,
                ),
                WorkflowTaskSpec(
                    task_id="recheck_decisions",
                    task_type=AgentTaskType.RECHECK_SKIPPED_CANDIDATES,
                    dependencies=("check_tracked",),
                ),
                WorkflowTaskSpec(
                    task_id="search_new",
                    task_type=AgentTaskType.SEARCH_CANDIDATES,
                    dependencies=("recheck_decisions",),
                    input_data={"query": query},
                ),
                WorkflowTaskSpec(
                    task_id="synthesize",
                    task_type=AgentTaskType.SYNTHESIZE_RESPONSE,
                    dependencies=("check_tracked", "recheck_decisions", "search_new"),
                ),
            )
        )
