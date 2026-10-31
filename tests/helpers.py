"""Shared test helpers: script a whole team of fake LLMs and run an incident through it."""

from collections.abc import AsyncIterator, Collection, Mapping, Sequence
from typing import Any

from incident_agent.llm.base import ModelSet
from incident_agent.llm.scripted import ScriptedModel, ToolCall, Turn, TurnScript
from incident_agent.runner import RunLimits, run_incident
from incident_agent.schemas import (
    Incident,
    IncidentReport,
    IncidentSynthesis,
    ReportReady,
    RunEvent,
)
from incident_agent.workers import WORKER_NAMES

LOGS, RUNBOOK, IMPACT, COMMS = WORKER_NAMES


def incident(**overrides: Any) -> Incident:
    fields = {
        "id": "inc-1",
        "title": "payments-api 503s",
        "severity": "sev1",
        "service": "payments-api",
        "description": "payments-api is returning 503s",
    }
    return Incident.model_validate({**fields, **overrides})


def call(worker: str, task: str = "investigate") -> ToolCall:
    return ToolCall(worker, {"input": task}, tool_use_id=f"call-{worker}")


def plan(rationale: str) -> ToolCall:
    return ToolCall("record_plan", {"rationale": rationale}, tool_use_id="plan")


def synthesis(workers: Collection[str] = (), **overrides: Any) -> Turn:
    fields: dict[str, Any] = {
        "summary": "Checkout is failing.",
        "probable_cause": "A deployment shrank the connection pool.",
        "confidence": "high",
        "recommended_actions": [{"action": "Roll back payments-api", "priority": "now"}],
        "comms_draft": None,
        "worker_contributions": [{"worker": w, "key_finding": f"from {w}"} for w in workers],
    }
    return Turn(
        tool_calls=[
            ToolCall(IncidentSynthesis.__name__, {**fields, **overrides}, tool_use_id="synthesis")
        ]
    )


def says(text: str) -> list[Turn]:
    """A worker that answers immediately, without using any tools."""
    return [Turn(text=text)]


class Team(ModelSet):
    """A ModelSet of ScriptedModels. Unscripted workers fail loudly if they are ever invoked."""

    def __init__(
        self, orchestrator: TurnScript, workers: Mapping[str, TurnScript] | None = None
    ) -> None:
        self.orchestrator_model = ScriptedModel(orchestrator)
        self.worker_models = {
            name: ScriptedModel((workers or {}).get(name, ())) for name in WORKER_NAMES
        }
        super().__init__(
            orchestrator=self.orchestrator_model,
            workers=self.worker_models,
            orchestrator_model_id="us.anthropic.claude-opus-5-5",
            worker_model_id="us.anthropic.claude-haiku-5-5",
        )

    def invoked_workers(self) -> set[str]:
        return {name for name, model in self.worker_models.items() if model.requests}


async def run(
    team: ModelSet, inc: Incident | None = None, limits: RunLimits | None = None
) -> list[RunEvent]:
    return [e async for e in stream(team, inc, limits)]


def stream(
    team: ModelSet, inc: Incident | None = None, limits: RunLimits | None = None
) -> AsyncIterator[RunEvent]:
    return run_incident(inc or incident(), team, limits=limits or RunLimits())


def report_from(events: Sequence[RunEvent]) -> IncidentReport:
    reports = [e.report for e in events if isinstance(e, ReportReady)]
    assert len(reports) == 1, f"expected exactly one report in {[e.type for e in events]}"
    return reports[0]


def text_of_messages(messages: Sequence[Any]) -> str:
    return "\n".join(
        part["text"]
        for message in messages
        for block in message["content"]
        for part in block.get("toolResult", {}).get("content", [])
        if "text" in part
    )
