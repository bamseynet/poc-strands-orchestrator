"""Ways a report or a stopped run could mislead. Each of these was a real bug found in review."""

import asyncio
import threading
import time
from typing import Any

import pytest

from incident_agent.config import Settings
from incident_agent.llm.factory import build_models
from incident_agent.llm.scripted import ToolCall, Turn
from incident_agent.runner import RunLimits, run_incident
from incident_agent.schemas import (
    RunEvent,
    SynthesisStarted,
    WorkerProgress,
)
from tests.helpers import IMPACT, LOGS, Team, call, incident, report_from, run, says, synthesis

TIMEOUT_S = 5


class TestAStoppedRunStopsSpendingOnBedrock:
    @pytest.mark.parametrize("stop", ["disconnect", "deadline"])
    async def test_a_workers_bedrock_stream_is_abandoned_at_the_next_chunk(
        self, stop: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A worker unwinds and clears its cancel signal before the thread reading from Bedrock next
        # looks at it. Without a signal that stays set, the model generates and bills to the end.
        chunks_read = 0
        entered = threading.Event()
        closed = threading.Event()

        def slow_chunks() -> Any:
            nonlocal chunks_read
            yield {"messageStart": {"role": "assistant"}}
            for _ in range(200):
                entered.set()
                chunks_read += 1
                time.sleep(0.01)
                yield {"contentBlockDelta": {"delta": {"text": "x "}}}

        class Stream:
            def __iter__(self) -> Any:
                return slow_chunks()

            def close(self) -> None:
                closed.set()

        def converse_stream(**request: Any) -> dict[str, Any]:
            if request["modelId"] == "us.anthropic.claude-haiku-5-5":
                return {"stream": Stream()}
            tool_use = {"toolUse": {"name": LOGS, "toolUseId": "c1"}}
            delta = {"toolUse": {"input": '{"input": "look"}'}}
            return {
                "stream": iter(
                    [
                        {"messageStart": {"role": "assistant"}},
                        {"contentBlockStart": {"start": tool_use, "contentBlockIndex": 0}},
                        {"contentBlockDelta": {"delta": delta, "contentBlockIndex": 0}},
                        {"contentBlockStop": {"contentBlockIndex": 0}},
                        {"messageStop": {"stopReason": "tool_use"}},
                    ]
                )
            }

        models = build_models(Settings(_env_file=None, llm_mode="bedrock"))
        for model in (models.orchestrator, models.workers[LOGS]):
            monkeypatch.setattr(model.client, "converse_stream", converse_stream)  # type: ignore[attr-defined]

        limits = RunLimits(timeout_s=0.2 if stop == "deadline" else 60)

        async def consume() -> None:
            async for _ in run_incident(incident(), models, limits=limits):
                pass

        task = asyncio.create_task(consume())
        while not entered.is_set():  # noqa: ASYNC110
            await asyncio.sleep(0.005)
        if stop == "disconnect":
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), TIMEOUT_S)
        await asyncio.sleep(0.2)

        assert closed.is_set(), f"never closed the stream; read {chunks_read} of 200 chunks"
        assert chunks_read < 100


class TestCompleteMeansComplete:
    async def test_a_second_brief_lost_to_a_same_turn_duplicate_is_flagged(self) -> None:
        release = asyncio.Event()

        async def hold() -> None:
            await release.wait()

        def orchestrate(request: Any) -> Turn:
            if len(request.messages) == 1:
                return Turn(
                    tool_calls=[
                        ToolCall(LOGS, {"input": "payments-api logs"}, tool_use_id="first"),
                        ToolCall(LOGS, {"input": "orders-db logs"}, tool_use_id="second"),
                    ]
                )
            return synthesis([LOGS])

        team = Team(orchestrate, {LOGS: says("payments logs read")})
        team.worker_models[LOGS].before_turn = hold
        asyncio.get_running_loop().call_later(0.05, release.set)

        report = report_from(await asyncio.wait_for(run(team), TIMEOUT_S))
        assert report.status == "degraded"
        assert any("orders-db logs" in note for note in report.degradation_notes)

    async def test_success_on_a_different_brief_does_not_excuse_a_failure(self) -> None:
        team = Team(
            [
                Turn(tool_calls=[ToolCall(LOGS, {"input": "orders-db logs"}, tool_use_id="a")]),
                Turn(tool_calls=[ToolCall(LOGS, {"input": "payments logs"}, tool_use_id="b")]),
                synthesis([LOGS]),
            ],
            {LOGS: [RuntimeError("blip"), Turn(text="payments logs read")]},
        )
        report = report_from(await run(team))
        assert report.status == "degraded"
        assert any("orders-db logs" in note for note in report.degradation_notes)

    async def test_a_worker_whose_every_tool_call_failed_is_flagged(self) -> None:
        team = Team(
            [Turn(tool_calls=[call(LOGS)]), synthesis([LOGS])],
            {
                LOGS: [
                    Turn(tool_calls=[ToolCall("search_logs", {"service": "no-such-service"})]),
                    Turn(text="I could not read any logs."),
                ]
            },
        )
        report = report_from(await run(team))
        assert report.status == "degraded"
        assert any(LOGS in note and "tool" in note for note in report.degradation_notes)

    async def test_crediting_a_worker_that_never_ran_is_flagged(self) -> None:
        team = Team([Turn(tool_calls=[call(LOGS)]), synthesis([LOGS, IMPACT])], {LOGS: says("x")})
        report = report_from(await run(team))
        assert report.status == "degraded"
        assert any(IMPACT in note for note in report.degradation_notes)

    async def test_a_synthesis_written_before_a_worker_answered_is_flagged(self) -> None:
        final = synthesis([LOGS])
        team = Team(
            [Turn(tool_calls=[call(LOGS), *final.tool_calls])],
            {LOGS: says("arrived too late")},
        )
        report = report_from(await run(team))
        assert report.status == "degraded"
        assert any(LOGS in note and "before" in note for note in report.degradation_notes)

    async def test_a_call_to_a_worker_that_does_not_exist_is_flagged(self) -> None:
        team = Team([Turn(tool_calls=[ToolCall("log_analyzer", {"input": "x"})]), synthesis()])
        report = report_from(await run(team))
        assert report.status == "degraded"
        assert any("log_analyzer" in note for note in report.degradation_notes)

    async def test_a_report_resting_on_no_specialists_says_so(self) -> None:
        report = report_from(await run(Team([synthesis()])))
        assert report.status == "degraded"
        assert any("No specialists" in note for note in report.degradation_notes)

    async def test_a_genuine_retry_of_the_same_brief_is_still_complete(self) -> None:
        team = Team(
            [
                Turn(tool_calls=[ToolCall(LOGS, {"input": "same"}, tool_use_id="a")]),
                Turn(tool_calls=[ToolCall(LOGS, {"input": "same"}, tool_use_id="b")]),
                synthesis([LOGS]),
            ],
            {LOGS: [RuntimeError("blip"), Turn(text="logs ok")]},
        )
        assert report_from(await run(team)).status == "complete"


class TestMalformedModelOutputCannotCrashARun:
    async def test_tool_input_that_is_not_an_object(self) -> None:
        team = Team(
            [Turn(tool_calls=[ToolCall(LOGS, "just a string")]), synthesis([LOGS])],  # type: ignore[arg-type]
            {LOGS: says("x")},
        )
        report = report_from(await asyncio.wait_for(run(team), TIMEOUT_S))
        assert report.worker_findings[0].task == "just a string"

    async def test_a_rationale_that_is_not_a_string(self) -> None:
        team = Team(
            [
                Turn(tool_calls=[ToolCall("record_plan", {"rationale": ["a", "b"]}), call(LOGS)]),
                synthesis([LOGS]),
            ],
            {LOGS: says("x")},
        )
        events = await asyncio.wait_for(run(team), TIMEOUT_S)
        assert report_from(events).worker_findings[0].output == "x"

    async def test_a_translator_bug_ends_the_run_with_its_findings_not_an_exception(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from incident_agent import translator as translator_module

        original = translator_module.EventTranslator.feed

        def explode_on_synthesis(self: Any, raw: Any) -> list[RunEvent]:
            if "result" in raw:
                raise ValueError("translator bug")
            return original(self, raw)

        monkeypatch.setattr(translator_module.EventTranslator, "feed", explode_on_synthesis)
        team = Team([Turn(tool_calls=[call(LOGS)]), synthesis([LOGS])], {LOGS: says("kept")})
        report = report_from(await asyncio.wait_for(run(team), TIMEOUT_S))
        assert report.worker_findings[0].output == "kept"


class TestRunnerEdges:
    async def test_a_second_cancel_during_the_grace_period_still_stops_everything(self) -> None:
        never = asyncio.Event()
        entered = asyncio.Event()

        async def hang() -> None:
            entered.set()
            await never.wait()

        team = Team([Turn(tool_calls=[call(LOGS)]), synthesis([LOGS])], {LOGS: says("never")})
        team.worker_models[LOGS].before_turn = hang

        async def consume() -> None:
            async for _ in run_incident(incident(), team, limits=RunLimits(cancel_grace_s=30)):
                pass

        task = asyncio.create_task(consume())
        await asyncio.wait_for(entered.wait(), TIMEOUT_S)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), TIMEOUT_S)
        await asyncio.sleep(0.2)
        current = asyncio.current_task()
        assert [t for t in asyncio.all_tasks() if t is not current] == []

    async def test_work_already_done_survives_a_deadline_that_passes_while_consuming(self) -> None:
        events: list[RunEvent] = []
        team = Team([synthesis([])])
        async for event in run_incident(incident(), team, limits=RunLimits(timeout_s=0.05)):
            events.append(event)
            await asyncio.sleep(0.06)
        assert report_from(events).synthesis_source == "orchestrator"


class TestNativeStructuredOutput:
    """On Opus 5.5 Strands skips forced tool_choice and uses `outputConfig`, replying with JSON."""

    async def test_synthesis_is_announced_when_it_arrives_as_json_text(self) -> None:
        import json

        payload = {
            "summary": "s",
            "probable_cause": "c",
            "confidence": "low",
            "recommended_actions": [],
            "worker_contributions": [],
        }
        team = Team([Turn(text="prose first"), Turn(text=json.dumps(payload))])
        events = await asyncio.wait_for(run(team), TIMEOUT_S)
        assert report_from(events).summary == "s"
        assert [e for e in events if isinstance(e, SynthesisStarted)]


class TestLiveness:
    async def test_a_workers_tool_call_is_reported_before_its_next_model_call(self) -> None:
        # The worker's second model call is released only by seeing its first tool call in the
        # stream. If progress were buffered until the worker finished, this would deadlock.
        seen_tool_call = asyncio.Event()
        calls = 0

        async def second_turn_waits() -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                await seen_tool_call.wait()

        team = Team(
            [Turn(tool_calls=[call(LOGS)]), synthesis([LOGS])],
            {
                LOGS: [
                    Turn(tool_calls=[ToolCall("list_log_sources", {})]),
                    Turn(text="done"),
                ]
            },
        )
        team.worker_models[LOGS].before_turn = second_turn_waits

        async def consume() -> None:
            async for event in run_incident(incident(), team):
                if isinstance(event, WorkerProgress) and event.step.kind == "tool_call":
                    seen_tool_call.set()

        await asyncio.wait_for(consume(), TIMEOUT_S)


async def test_a_failure_while_building_the_orchestrator_becomes_an_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from incident_agent import runner

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("bad model config")

    monkeypatch.setattr(runner, "build_orchestrator", boom)
    events = await run(Team([Turn()]), incident())
    assert events[-1].type == "run_failed"
    assert "bad model config" in events[-1].error
