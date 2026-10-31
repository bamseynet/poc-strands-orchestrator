"""Turns the raw Strands event stream into this project's typed `RunEvent`s.

Strands surfaces the sub-agents' own events through `tool_stream_event`, which is what lets one
consumer watch all the parallel workers at once. This is the only module that knows those shapes.
"""

import json
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from incident_agent.orchestrator import RECORD_PLAN_TOOL, SYNTHESIS_TOOL
from incident_agent.redaction import redact
from incident_agent.schemas import (
    OrchestratorTurn,
    PlannedCall,
    PlanRecorded,
    RunEvent,
    SynthesisStarted,
    TraceStep,
    Usage,
    WorkerFinished,
    WorkerProgress,
    WorkerRun,
    WorkerStarted,
    WorkerTextDelta,
)

_MAX_TRACE_DETAIL_CHARS = 2000
# Anything else ending a worker's response (a refusal, a guardrail) means its text is not a finding.
_CLEAN_STOP_REASONS = frozenset({"end_turn", "stop_sequence", "tool_use"})


def _truncate(text: str) -> str:
    if len(text) <= _MAX_TRACE_DETAIL_CHARS:
        return text
    return f"{text[:_MAX_TRACE_DETAIL_CHARS]} … [truncated]"


def _text_of(content: Iterable[Mapping[str, Any]]) -> str:
    return "".join(block.get("text", "") for block in content)


def _usage_of(message: Mapping[str, Any]) -> Usage:
    usage = message.get("metadata", {}).get("usage", {})
    return Usage(
        input_tokens=usage.get("inputTokens", 0), output_tokens=usage.get("outputTokens", 0)
    )


def _task_of(tool_input: Any) -> str:
    """The brief in a worker call. Models usually send an object, but Strands also accepts text."""
    return str(tool_input.get("input", "")) if isinstance(tool_input, Mapping) else str(tool_input)


def _rationale_of(tool_use: Mapping[str, Any]) -> str | None:
    tool_input = tool_use["input"]
    rationale = tool_input.get("rationale") if isinstance(tool_input, Mapping) else None
    return rationale if isinstance(rationale, str) else None


class EventTranslator:
    """Feed it raw events in order; it returns the typed events and tracks the run's state."""

    def __init__(
        self, worker_names: Iterable[str], clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._worker_names = frozenset(worker_names)
        self._clock = clock
        self._started_at = clock()
        self._turn = 0
        self._synthesis_announced = False
        self._runs: dict[str, WorkerRun] = {}
        self._stop_reasons: dict[str, str] = {}
        self._tool_names: dict[tuple[str, str], str] = {}
        self.orchestrator_usage = Usage()
        self.last_orchestrator_text = ""
        self.synthesis_turn: int | None = None
        self.unknown_tool_calls: list[str] = []

    @property
    def runs(self) -> list[WorkerRun]:
        """Every worker call so far, in the order they were requested."""
        return list(self._runs.values())

    def elapsed_ms(self) -> int:
        return int((self._clock() - self._started_at) * 1000)

    def feed(self, raw: Mapping[str, Any]) -> list[RunEvent]:
        if stream_event := raw.get("tool_stream_event"):
            return self._feed_tool_stream(stream_event)
        if "start_event_loop" in raw:
            self._turn += 1
            return [OrchestratorTurn(turn=self._turn)]
        if "message" in raw:
            message = raw["message"]
            if message["role"] == "assistant":
                return self._on_orchestrator_message(message)
            return self._on_tool_results(message)
        if "structured_output" in raw:
            # When the model replies with JSON text, as Opus 5.5 does on a retry, no tool call
            # announced the synthesis.
            self.synthesis_turn = self.synthesis_turn or self._turn
            return self._announce_synthesis()
        start = raw.get("event", {}).get("contentBlockStart", {}).get("start", {})
        if start.get("toolUse", {}).get("name") == SYNTHESIS_TOOL:
            return self._announce_synthesis()
        return []

    def finalise(self, reason: str) -> list[RunEvent]:
        """Close off calls still in flight when the run ended, so none silently disappear."""
        return [self._finish(run, error=reason) for run in self.runs if run.status == "running"]

    def _announce_synthesis(self) -> list[RunEvent]:
        if self._synthesis_announced:
            return []
        self._synthesis_announced = True
        return [SynthesisStarted()]

    def _on_orchestrator_message(self, message: Mapping[str, Any]) -> list[RunEvent]:
        usage = _usage_of(message)
        self.orchestrator_usage.input_tokens += usage.input_tokens
        self.orchestrator_usage.output_tokens += usage.output_tokens

        content = message["content"]
        text = _text_of(content).strip()
        if text:
            self.last_orchestrator_text = text

        tool_uses = [block["toolUse"] for block in content if "toolUse" in block]
        calls = [
            PlannedCall(
                tool_use_id=tool_use["toolUseId"],
                worker=tool_use["name"],
                task=_task_of(tool_use["input"]),
            )
            for tool_use in tool_uses
            if tool_use["name"] in self._worker_names
        ]
        recorded = next(
            (_rationale_of(use) for use in tool_uses if use["name"] == RECORD_PLAN_TOOL), None
        )
        for tool_use in tool_uses:
            if tool_use["name"] == SYNTHESIS_TOOL:
                self.synthesis_turn = self._turn
            elif tool_use["name"] not in {*self._worker_names, RECORD_PLAN_TOOL}:
                self.unknown_tool_calls.append(tool_use["name"])
        if not calls and recorded is None:
            return []

        # Text between tool calls can come back hidden on newer models, so it is only a fallback.
        events: list[RunEvent] = [
            PlanRecorded(turn=self._turn, rationale=recorded or text or None, calls=calls)
        ]
        for planned in calls:
            self._runs[planned.tool_use_id] = WorkerRun(
                tool_use_id=planned.tool_use_id,
                worker=planned.worker,
                turn=self._turn,
                task=planned.task,
                started_ms=self.elapsed_ms(),
            )
            events.append(
                WorkerStarted(
                    tool_use_id=planned.tool_use_id,
                    worker=planned.worker,
                    turn=self._turn,
                    task=planned.task,
                )
            )
        return events

    def _on_tool_results(self, message: Mapping[str, Any]) -> list[RunEvent]:
        """Reconcile with the authoritative results, which arrive only after the whole batch.

        Workers normally finished earlier, from their own stream. This catches the ones that never
        got to stream anything, and any verdict the framework reached after the worker's last event.
        """
        events: list[RunEvent] = []
        for block in message["content"]:
            result = block.get("toolResult")
            run = self._runs.get(result["toolUseId"]) if result else None
            if result is None or run is None:
                continue
            text = _text_of(result["content"]).strip()
            if result.get("status") == "error" and run.status != "failed":
                events.append(self._finish(run, error=text or "The worker failed."))
            elif run.status == "running":
                events.append(self._finish_with_text(run, text))
        return events

    def _feed_tool_stream(self, stream_event: Mapping[str, Any]) -> list[RunEvent]:
        run = self._runs.get(stream_event["tool_use"]["toolUseId"])
        data = stream_event["data"]
        if run is None or not isinstance(data, Mapping):
            return []
        if "data" in data:
            return [
                WorkerTextDelta(tool_use_id=run.tool_use_id, worker=run.worker, text=data["data"])
            ]
        if "force_stop" in data:
            return [self._finish(run, error=str(data.get("force_stop_reason", "stopped")))]
        if stop := data.get("event", {}).get("messageStop"):
            self._stop_reasons[run.tool_use_id] = stop["stopReason"]
            return []
        if "message" in data:
            message = data["message"]
            if message["role"] == "assistant":
                return self._on_worker_message(run, message)
            return self._on_worker_tool_results(run, message)
        return []

    def _on_worker_message(self, run: WorkerRun, message: Mapping[str, Any]) -> list[RunEvent]:
        if run.status != "running":
            return []  # Stopped by force_stop: a late message must not revive it.
        usage = _usage_of(message)
        run.usage.input_tokens += usage.input_tokens
        run.usage.output_tokens += usage.output_tokens

        events: list[RunEvent] = []
        content = message["content"]
        text = _text_of(content).strip()
        if text:
            events.append(self._step(run, "text", detail=text))
        tool_uses = [block["toolUse"] for block in content if "toolUse" in block]
        for tool_use in tool_uses:
            self._tool_names[(run.tool_use_id, tool_use["toolUseId"])] = tool_use["name"]
            detail = json.dumps(tool_use["input"])
            events.append(self._step(run, "tool_call", name=tool_use["name"], detail=detail))
        if not tool_uses:
            events.append(self._finish_with_text(run, text))
        return events

    def _on_worker_tool_results(self, run: WorkerRun, message: Mapping[str, Any]) -> list[RunEvent]:
        events: list[RunEvent] = []
        for block in message["content"]:
            if result := block.get("toolResult"):
                events.append(
                    self._step(
                        run,
                        "tool_result",
                        name=self._tool_names.get((run.tool_use_id, result["toolUseId"])),
                        detail=_text_of(result["content"]),
                        is_error=result.get("status") == "error",
                    )
                )
        return events

    def _step(
        self,
        run: WorkerRun,
        kind: Any,
        *,
        detail: str,
        name: str | None = None,
        is_error: bool = False,
    ) -> RunEvent:
        step = TraceStep(
            kind=kind,
            name=name,
            detail=_truncate(detail),
            is_error=is_error,
            at_ms=self.elapsed_ms(),
        )
        run.trace.append(step)
        return WorkerProgress(tool_use_id=run.tool_use_id, worker=run.worker, step=step)

    def _finish_with_text(self, run: WorkerRun, text: str) -> RunEvent:
        stop_reason = self._stop_reasons.get(run.tool_use_id, "end_turn")
        if stop_reason not in _CLEAN_STOP_REASONS:
            return self._finish(run, error=f"The model stopped early: {stop_reason}.")
        if not text:
            return self._finish(run, error="The worker returned no output.")
        return self._finish(run, output=text)

    def _finish(self, run: WorkerRun, *, output: str = "", error: str | None = None) -> RunEvent:
        run.status = "failed" if error is not None else "ok"
        run.output = output
        run.error = redact(error) if error is not None else None
        run.duration_ms = self.elapsed_ms() - run.started_ms
        # Copy: this run can still be corrected later, and an emitted event must not change.
        return WorkerFinished(run=run.model_copy(deep=True))
