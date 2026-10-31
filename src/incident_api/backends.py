"""Where the agent runs. The API only needs a stream of events, wherever they come from."""

from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from incident_agent.config import Settings
from incident_agent.llm.factory import build_models
from incident_agent.runner import RunLimits, run_incident
from incident_agent.schemas import Incident, RunEvent


class AgentBackend(Protocol):
    def stream(
        self, incident: Incident, simulate_failures: Sequence[str]
    ) -> AsyncIterator[RunEvent]: ...


class InProcessBackend:
    """Runs the agent in this process: what `make dev` uses."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def stream(
        self, incident: Incident, simulate_failures: Sequence[str]
    ) -> AsyncIterator[RunEvent]:
        limits = RunLimits(
            turns=self._settings.max_total_turns,
            total_tokens=self._settings.max_total_tokens,
            timeout_s=self._settings.run_timeout_s,
        )
        models = build_models(self._settings, simulate_failures=simulate_failures)
        return run_incident(incident, models, limits=limits)
