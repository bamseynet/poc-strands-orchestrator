"""Assembles the final report, and refuses to take the LLM's word for its completeness.

The orchestrator writes the conclusions, but the worker findings come straight from the run. The
report is marked degraded wherever the run shows the synthesis cannot be relied on, and a failed
synthesis still leaves the raw findings. These are structural checks: they cannot tell whether the
LLM's reasoning from the evidence is sound.
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
_MAX_BRIEF_IN_NOTE = 200
_FALLBACK_SUMMARY = (
    "The orchestrator did not complete its synthesis. Read the specialist findings directly."
)


def _unrecovered_failures(runs: list[WorkerRun]) -> list[WorkerRun]:
    """Failed calls with no success on the same brief.

    A success on a different brief says nothing about what the failed call was asked. A retry that
    rewords its brief is flagged too: better a false alarm than missing evidence going unnoticed.
    """
    return [
        run
        for run in runs
        if run.status == "failed"
        and not any(
            other.status == "ok" and other.worker == run.worker and other.task == run.task
            for other in runs
        )
    ]


def _failure_notes(runs: list[WorkerRun]) -> list[str]:
    briefs_by_worker = {
        worker: {run.task for run in runs if run.worker == worker}
        for worker in {run.worker for run in runs}
    }
    notes = []
    for run in _unrecovered_failures(runs):
        note = f"Specialist {run.worker} failed, so its evidence is missing from this report."
        if len(briefs_by_worker[run.worker]) > 1:
            note = f"Specialist {run.worker} failed on this brief: {run.task[:_MAX_BRIEF_IN_NOTE]}"
        if note not in notes:
            notes.append(note)
    return notes


def _synthesis_notes(
    runs: list[WorkerRun], synthesis: IncidentSynthesis, synthesis_turn: int | None
) -> list[str]:
    """Places where the LLM's account of the run does not match what really happened."""
    ran = {run.worker for run in runs}
    credited = {contribution.worker for contribution in synthesis.worker_contributions}
    notes = [
        f"The synthesis credits {worker}, which never ran. Treat that claim with suspicion."
        for worker in sorted(credited - ran)
    ]
    if synthesis_turn is not None:
        notes.extend(
            f"The synthesis was written before {worker} answered, so it cannot reflect that "
            "finding. Read its raw finding below."
            for worker in dict.fromkeys(r.worker for r in runs if r.turn >= synthesis_turn)
        )
    return notes


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
    synthesis_turn: int | None,
    unknown_tool_calls: list[str],
    failure_reason: str | None,
    fallback_text: str,
    orchestrator_usage: Usage,
    orchestrator_model_id: str,
    worker_model_id: str,
    duration_ms: int,
) -> IncidentReport:
    notes = _failure_notes(runs)
    notes.extend(
        f"Specialist {run.worker} made tool calls that all failed, so its answer has no evidence "
        "behind it."
        for run in runs
        if run.status == "ok"
        and (results := [step for step in run.trace if step.kind == "tool_result"])
        and all(step.is_error for step in results)
    )
    notes.extend(
        f"The orchestrator asked for {name}, which does not exist, so that work never happened."
        for name in dict.fromkeys(unknown_tool_calls)
    )
    if failure_reason:
        notes.append(failure_reason)
    if synthesis:
        notes.extend(_synthesis_notes(runs, synthesis, synthesis_turn))
        if not runs:
            notes.append("No specialists were called, so this rests on the incident text alone.")

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
