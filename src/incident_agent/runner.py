"""Runs one incident from start to finish and streams what happens as typed events."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid4

from strands.types.agent import Limits

from incident_agent.llm.base import ModelSet
from incident_agent.orchestrator import build_orchestrator, incident_prompt
from incident_agent.redaction import redact
from incident_agent.report import assemble_report
from incident_agent.schemas import (
    Incident,
    IncidentSynthesis,
    ReportReady,
    RunEvent,
    RunFailed,
    RunStarted,
)
from incident_agent.translator import EventTranslator
from incident_agent.workers import WORKER_NAMES


@dataclass(frozen=True)
class RunLimits:
    """Guard rails against a confused model, a hung dependency or a runaway bill.

    Strands counts `turns` and `total_tokens` across the whole request, so the workers draw on the
    same budget as the orchestrator. Size them for the team, not for the orchestrator alone.
    """

    turns: int = 40
    total_tokens: int = 500_000
    timeout_s: float = 300.0
    structured_output_attempts: int = 2


async def run_incident(
    incident: Incident,
    models: ModelSet,
    *,
    limits: RunLimits | None = None,
    run_id: str | None = None,
) -> AsyncIterator[RunEvent]:
    """Stream a run. Ends with `ReportReady`, or `RunFailed` if there is nothing worth reporting.

    Never raises for an agent or model failure: those become events, because by then a client is
    usually mid-stream and can no longer be sent an HTTP error.
    """
    limits = limits or RunLimits()
    run_id = run_id or uuid4().hex
    translator = EventTranslator(WORKER_NAMES)
    orchestrator = build_orchestrator(
        models, trace_attributes={"incident.id": incident.id, "session.id": run_id}
    )

    yield RunStarted(
        run_id=run_id,
        incident=incident,
        orchestrator_model=models.orchestrator_model_id,
        worker_model=models.worker_model_id,
        available_workers=list(WORKER_NAMES),
    )

    # Strands types this as an iterator, but it is a generator: closing it cancels running workers.
    stream = cast(
        AsyncGenerator[Any, None],
        orchestrator.stream_async(
            incident_prompt(incident),
            limits=Limits(
                turns=limits.turns,
                total_tokens=limits.total_tokens,
                structured_output_attempts=limits.structured_output_attempts,
            ),
        ),
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limits.timeout_s
    synthesis: IncidentSynthesis | None = None
    failure: str | None = None
    try:
        while True:
            try:
                # Timed per step: an asyncio.timeout() block held open across the `yield` below
                # could fire while the consumer is running, and be raised in the consumer's code.
                raw = await asyncio.wait_for(anext(stream), deadline - loop.time())
            except StopAsyncIteration:
                break
            except TimeoutError:
                failure = f"The run hit its {limits.timeout_s:g}s deadline before finishing."
                break
            except Exception as error:
                failure = redact(f"The orchestrator failed: {error}")
                break

            for event in translator.feed(raw):
                yield event
            if result := raw.get("result"):
                if isinstance(result.structured_output, IncidentSynthesis):
                    synthesis = result.structured_output
                else:
                    failure = f"The orchestrator stopped without a report: {result.stop_reason}."
    finally:
        await stream.aclose()

    for event in translator.finalise(failure or "The run ended before this worker finished."):
        yield event

    if synthesis is None and not translator.runs and not translator.last_orchestrator_text:
        yield RunFailed(error=failure or "The orchestrator produced nothing.")
        return

    yield ReportReady(
        report=assemble_report(
            run_id=run_id,
            incident=incident,
            runs=translator.runs,
            synthesis=synthesis,
            failure_reason=failure,
            fallback_text=translator.last_orchestrator_text,
            orchestrator_usage=translator.orchestrator_usage,
            orchestrator_model_id=models.orchestrator_model_id,
            worker_model_id=models.worker_model_id,
            duration_ms=translator.elapsed_ms(),
        )
    )
