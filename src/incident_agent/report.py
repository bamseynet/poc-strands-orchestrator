"""Assembles the final report, and refuses to take the LLM's word for its completeness.

The orchestrator writes the conclusions, but the worker findings come straight from the run. A
synthesis that skips a worker is flagged, and a failed synthesis still leaves the raw findings.
"""

from incident_agent import pricing
from incident_agent.schemas import (
    CostEstimate,
    Incident,
    IncidentReport,
    IncidentSynthesis,
    Usage,
    WorkerRun,
)

COMMS_WORKER = "comms_drafter"
_FALLBACK_CAUSE = "Undetermined: the orchestrator did not complete its synthesis."
_FALLBACK_SUMMARY = (
    "The orchestrator did not complete its synthesis. Read the specialist findings directly."
)


def _workers_with_only_failures(runs: list[WorkerRun]) -> list[str]:
    """Workers with no successful call. One the orchestrator retried successfully is fine."""
    succeeded = {run.worker for run in runs if run.status == "ok"}
    return list(dict.fromkeys(r.worker for r in runs if r.worker not in succeeded))


def _cost(
    runs: list[WorkerRun],
    orchestrator_usage: Usage,
    orchestrator_model_id: str,
    worker_model_id: str,
) -> CostEstimate:
    by_worker: dict[str, Usage] = {}
    for run in runs:
        total = by_worker.setdefault(run.worker, Usage())
        total.input_tokens += run.usage.input_tokens
        total.output_tokens += run.usage.output_tokens
    return pricing.estimate(
        [
            ("orchestrator", orchestrator_model_id, orchestrator_usage),
            *((worker, worker_model_id, usage) for worker, usage in by_worker.items()),
        ]
    )


def assemble_report(
    *,
    run_id: str,
    incident: Incident,
    runs: list[WorkerRun],
    synthesis: IncidentSynthesis | None,
    failure_reason: str | None,
    fallback_text: str,
    orchestrator_usage: Usage,
    orchestrator_model_id: str,
    worker_model_id: str,
    duration_ms: int,
) -> IncidentReport:
    notes = [
        f"Specialist {worker} failed, so its evidence is missing from this report."
        for worker in _workers_with_only_failures(runs)
    ]
    if failure_reason:
        notes.append(failure_reason)

    succeeded = list(dict.fromkeys(run.worker for run in runs if run.status == "ok"))
    acknowledged = {c.worker for c in synthesis.worker_contributions} if synthesis else set()
    unacknowledged = [worker for worker in succeeded if synthesis and worker not in acknowledged]
    notes.extend(
        f"The synthesis does not account for {worker}. Read its raw finding below."
        for worker in unacknowledged
    )

    comms_outputs = [
        run.output for run in runs if run.worker == COMMS_WORKER and run.status == "ok"
    ]
    written_comms = synthesis.comms_draft if synthesis else None

    return IncidentReport(
        run_id=run_id,
        incident=incident,
        status="degraded" if notes or synthesis is None else "complete",
        synthesis_source="orchestrator" if synthesis else "fallback",
        summary=synthesis.summary if synthesis else (fallback_text or _FALLBACK_SUMMARY),
        probable_cause=synthesis.probable_cause if synthesis else _FALLBACK_CAUSE,
        confidence=synthesis.confidence if synthesis else "low",
        recommended_actions=synthesis.recommended_actions if synthesis else [],
        comms_draft=written_comms or (comms_outputs[-1] if comms_outputs else None),
        worker_findings=runs,
        unacknowledged_workers=unacknowledged,
        degradation_notes=notes,
        cost=_cost(runs, orchestrator_usage, orchestrator_model_id, worker_model_id),
        duration_ms=duration_ms,
    )
