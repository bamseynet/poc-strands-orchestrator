"""The agent's public data shapes: incident in, typed events out, report at the end.

The API and the UI depend on these and never on raw Strands events, so a framework upgrade can only
ever break `translator.py`.
"""

from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, TypeAdapter

Severity = Literal["sev1", "sev2", "sev3", "sev4"]


class Incident(BaseModel):
    """An incident as reported by an alert or a person. Its text is untrusted input."""

    id: str = Field(default_factory=lambda: f"inc-{uuid4().hex[:8]}", max_length=64)
    title: str = Field(min_length=1, max_length=200)
    severity: Severity
    service: str | None = Field(default=None, max_length=64)
    description: str = Field(min_length=1, max_length=4000)
    source: str | None = Field(default=None, max_length=64)
    started_at: str | None = Field(default=None, max_length=32)


class RecommendedAction(BaseModel):
    action: str
    priority: Literal["now", "soon", "later"]


class WorkerContribution(BaseModel):
    worker: str = Field(description="The specialist's tool name, e.g. log_analysis.")
    key_finding: str = Field(description="What this specialist established, or why it failed.")


class IncidentSynthesis(BaseModel):
    """The final incident report. Call this tool once, after the last specialist has answered."""

    summary: str = Field(description="What is happening and who is affected, in 2-4 sentences.")
    probable_cause: str = Field(description="The most likely cause, with the evidence for it.")
    confidence: Literal["low", "medium", "high"]
    recommended_actions: list[RecommendedAction] = Field(
        description="Concrete next steps for the responders, most urgent first."
    )
    comms_draft: str | None = Field(
        default=None, description="Stakeholder message from comms_drafter, if it was called."
    )
    worker_contributions: list[WorkerContribution] = Field(
        description="One entry for every specialist called, including any that failed."
    )


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class TraceStep(BaseModel):
    """One thing a worker did. `detail` is truncated for display; the model saw the full text."""

    kind: Literal["text", "tool_call", "tool_result"]
    name: str | None = None
    detail: str
    is_error: bool = False
    at_ms: int


class WorkerRun(BaseModel):
    """One call of one worker. A worker the orchestrator retried has several."""

    tool_use_id: str
    worker: str
    turn: int
    task: str
    status: Literal["running", "ok", "failed"] = "running"
    output: str = ""
    error: str | None = None
    usage: Usage = Field(default_factory=Usage)
    trace: list[TraceStep] = Field(default_factory=list)
    started_ms: int
    duration_ms: int | None = None


class CostLine(BaseModel):
    label: str
    model_id: str
    input_tokens: int
    output_tokens: int
    usd: float | None = Field(description="None when no price is known for this model.")


class CostEstimate(BaseModel):
    lines: list[CostLine]
    total_usd: float | None = Field(description="None if any line could not be priced.")
    basis: str


class IncidentReport(BaseModel):
    run_id: str
    incident: Incident
    status: Literal["complete", "degraded"]
    synthesis_source: Literal["orchestrator", "fallback"]
    summary: str
    probable_cause: str
    confidence: Literal["low", "medium", "high"]
    recommended_actions: list[RecommendedAction]
    comms_draft: str | None
    worker_findings: list[WorkerRun]
    unacknowledged_workers: list[str]
    degradation_notes: list[str]
    cost: CostEstimate
    duration_ms: int


class RunStarted(BaseModel):
    type: Literal["run_started"] = "run_started"
    run_id: str
    incident: Incident
    orchestrator_model: str
    worker_model: str
    available_workers: list[str]


class OrchestratorTurn(BaseModel):
    """The orchestrator is deciding what to do next."""

    type: Literal["orchestrator_turn"] = "orchestrator_turn"
    turn: int


class PlannedCall(BaseModel):
    tool_use_id: str
    worker: str
    task: str


class PlanRecorded(BaseModel):
    """What the orchestrator decided this turn. Calls in one entry run in parallel."""

    type: Literal["plan_recorded"] = "plan_recorded"
    turn: int
    rationale: str | None
    calls: list[PlannedCall]


class WorkerStarted(BaseModel):
    type: Literal["worker_started"] = "worker_started"
    tool_use_id: str
    worker: str
    turn: int
    task: str


class WorkerTextDelta(BaseModel):
    type: Literal["worker_text_delta"] = "worker_text_delta"
    tool_use_id: str
    worker: str
    text: str


class WorkerProgress(BaseModel):
    type: Literal["worker_progress"] = "worker_progress"
    tool_use_id: str
    worker: str
    step: TraceStep


class WorkerFinished(BaseModel):
    """A worker call ended. May repeat for one call if a later signal corrects it: last one wins."""

    type: Literal["worker_finished"] = "worker_finished"
    run: WorkerRun


class SynthesisStarted(BaseModel):
    type: Literal["synthesis_started"] = "synthesis_started"


class ReportReady(BaseModel):
    type: Literal["report_ready"] = "report_ready"
    report: IncidentReport


class RunFailed(BaseModel):
    """The run produced nothing usable. If any worker finished you get a degraded report instead."""

    type: Literal["run_failed"] = "run_failed"
    error: str


RunEvent = Annotated[
    RunStarted
    | OrchestratorTurn
    | PlanRecorded
    | WorkerStarted
    | WorkerTextDelta
    | WorkerProgress
    | WorkerFinished
    | SynthesisStarted
    | ReportReady
    | RunFailed,
    Field(discriminator="type"),
]
RUN_EVENT_ADAPTER: TypeAdapter[RunEvent] = TypeAdapter(RunEvent)
