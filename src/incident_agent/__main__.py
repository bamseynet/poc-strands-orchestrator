"""Run one incident from a terminal: `python -m incident_agent --sample checkout-5xx`."""

import argparse
import asyncio
import sys
from collections.abc import Sequence

from incident_agent import knowledge
from incident_agent.config import Settings
from incident_agent.llm.factory import build_models
from incident_agent.runner import RunLimits, run_incident
from incident_agent.schemas import (
    Incident,
    IncidentReport,
    PlanRecorded,
    ReportReady,
    RunEvent,
    RunFailed,
    RunStarted,
    SynthesisStarted,
    WorkerFinished,
    WorkerProgress,
    WorkerStarted,
)
from incident_agent.telemetry import configure_telemetry
from incident_agent.workers import WORKER_NAMES


def _format_report(report: IncidentReport) -> str:
    lines = [
        "",
        f"=== Report ({report.status}, {report.duration_ms} ms) ===",
        f"Summary: {report.summary}",
        f"Probable cause ({report.confidence} confidence): {report.probable_cause}",
        "Recommended actions:",
        *(f"  [{a.priority}] {a.action}" for a in report.recommended_actions),
    ]
    if report.comms_draft:
        lines += ["Comms draft:", report.comms_draft]
    lines += [f"Degraded: {note}" for note in report.degradation_notes]
    total = report.cost.total_usd
    lines.append(f"Estimated cost: {'unknown' if total is None else f'${total:.4f}'}")
    return "\n".join(lines)


def _format_event(event: RunEvent) -> str | None:
    match event:
        case RunStarted():
            return (
                f"Incident {event.incident.id}: {event.incident.title}\n"
                f"Orchestrator {event.orchestrator_model}, workers {event.worker_model}"
            )
        case PlanRecorded():
            workers = ", ".join(call.worker for call in event.calls) or "none yet"
            return (
                f"[plan, turn {event.turn}] {event.rationale or '(no rationale)'}\n  -> {workers}"
            )
        case WorkerStarted():
            return f"[{event.worker}] started"
        case WorkerProgress() if event.step.kind == "tool_call":
            return f"[{event.worker}] calling {event.step.name} {event.step.detail}"
        case WorkerFinished():
            run = event.run
            outcome = (
                run.error if run.status == "failed" else f"{run.usage.output_tokens} tokens out"
            )
            return f"[{run.worker}] {run.status} in {run.duration_ms} ms: {outcome}"
        case SynthesisStarted():
            return "[orchestrator] synthesising the report"
        case ReportReady():
            return _format_report(event.report)
        case RunFailed():
            return f"Run failed: {event.error}"
        case _:
            return None


async def _run(
    incident: Incident, settings: Settings, failures: Sequence[str], as_json: bool
) -> int:
    limits = RunLimits(
        turns=settings.max_total_turns,
        total_tokens=settings.max_total_tokens,
        timeout_s=settings.run_timeout_s,
    )
    exit_code = 0
    models = build_models(settings, simulate_failures=failures)
    async for event in run_incident(incident, models, limits=limits):
        if isinstance(event, RunFailed):
            exit_code = 1
        text = event.model_dump_json() if as_json else _format_event(event)
        if text is not None:
            print(text, flush=True)
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    samples = {sample["id"]: sample for sample in knowledge.sample_incidents()}
    parser = argparse.ArgumentParser(prog="incident_agent", description=__doc__)
    parser.add_argument("--list", action="store_true", help="list the sample incidents")
    parser.add_argument("--sample", metavar="ID", help="the sample incident to run")
    parser.add_argument(
        "--simulate-failure",
        action="append",
        default=[],
        choices=WORKER_NAMES,
        help="make a worker fail (LLM_MODE=fake only)",
    )
    parser.add_argument("--json", action="store_true", help="print raw events as JSON lines")
    args = parser.parse_args(argv)

    if args.list:
        for sample_id, sample in samples.items():
            print(f"{sample_id}: {sample['title']}")
        return 0
    if args.sample is None:
        parser.error(f"--sample is required; choose from {', '.join(samples)}")
    if args.sample not in samples:
        parser.error(f"unknown sample {args.sample!r}; choose from {', '.join(samples)}")

    settings = Settings()
    if args.simulate_failure and settings.llm_mode != "fake":
        parser.error("--simulate-failure needs LLM_MODE=fake")
    configure_telemetry(settings)
    incident = Incident.model_validate(samples[args.sample])
    return asyncio.run(_run(incident, settings, args.simulate_failure, args.json))


if __name__ == "__main__":
    sys.exit(main())
