from collections.abc import Mapping

from conference_scout.schemas.common import AgentTaskType
from conference_scout.workflows.engine import TaskHandler


class ConferenceWorkflowHandlers:
    """Белый список обработчиков конференционного workflow."""

    def __init__(self, handlers: Mapping[AgentTaskType, TaskHandler]) -> None:
        self._handlers = dict(handlers)

    def as_mapping(self) -> dict[AgentTaskType, TaskHandler]:
        return dict(self._handlers)
