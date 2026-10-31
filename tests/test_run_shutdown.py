"""A run must stop cleanly however it ends: nothing left running, nothing left spending."""

import asyncio
import logging

import pytest

from incident_agent.llm.scripted import Turn
from incident_agent.runner import RunLimits, run_incident
from incident_agent.schemas import RunEvent, WorkerTextDelta
from tests.helpers import IMPACT, LOGS, Team, call, incident, report_from, run, says, synthesis

TIMEOUT_S = 5


def other_tasks() -> list[str]:
    current = asyncio.current_task()
    return [task.get_name() for task in asyncio.all_tasks() if task is not current]


class MidFlight:
    """A team whose workers signal once inside a model call, then take a while to finish."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.team = Team(
            [Turn(tool_calls=[call(LOGS), call(IMPACT)]), synthesis([LOGS, IMPACT])],
            {LOGS: says("logs"), IMPACT: says("impact")},
        )
        for name in (LOGS, IMPACT):
            self.team.worker_models[name].before_turn = self._enter

    async def _enter(self) -> None:
        self.entered.set()
        await asyncio.sleep(0.05)

    async def disconnect_by_cancelling(self, limits: RunLimits | None = None) -> None:
        """Cancel the consuming task, which is what a server does when a client goes away."""

        async def consume() -> None:
            async for _ in run_incident(incident(), self.team, limits=limits):
                pass

        task = asyncio.create_task(consume())
        await asyncio.wait_for(self.entered.wait(), TIMEOUT_S)
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), TIMEOUT_S)

    async def disconnect_by_closing(self) -> None:
        """Stop iterating part way and close the generator while workers are mid-stream."""
        stream = run_incident(incident(), self.team)
        async for event in stream:
            if isinstance(event, WorkerTextDelta):
                break
        await asyncio.wait_for(stream.aclose(), TIMEOUT_S)


DISCONNECTS = ["disconnect_by_cancelling", "disconnect_by_closing"]


@pytest.mark.parametrize("disconnect", DISCONNECTS)
class TestDisconnectingMidRun:
    async def test_leaves_no_tasks_behind(self, disconnect: str) -> None:
        await getattr(MidFlight(), disconnect)()
        await asyncio.sleep(0.2)  # longer than Strands' cancel-signal polling interval
        assert other_tasks() == []

    async def test_shuts_down_inside_otel_context_rules(
        self, disconnect: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Strands holds OTel context managers open inside generators. Closing those generators from
        # a different task logs "Failed to detach context" and can lose spans.
        with caplog.at_level(logging.ERROR, logger="opentelemetry.context"):
            await getattr(MidFlight(), disconnect)()
            await asyncio.sleep(0.2)
        assert "Failed to detach" not in caplog.text

    async def test_makes_no_further_model_calls(self, disconnect: str) -> None:
        mid_flight = MidFlight()
        await getattr(mid_flight, disconnect)()
        await asyncio.sleep(0.2)
        assert len(mid_flight.team.orchestrator_model.requests) == 1, "no synthesis call may happen"


class TestAWorkerThatIgnoresCancellation:
    async def test_is_stopped_after_the_grace_period(self) -> None:
        never = asyncio.Event()
        entered = asyncio.Event()

        async def hang() -> None:
            entered.set()
            await never.wait()

        team = Team([Turn(tool_calls=[call(LOGS)]), synthesis([LOGS])], {LOGS: says("never")})
        team.worker_models[LOGS].before_turn = hang

        async def consume() -> None:
            limits = RunLimits(cancel_grace_s=0.1)
            async for _ in run_incident(incident(), team, limits=limits):
                pass

        task = asyncio.create_task(consume())
        await asyncio.wait_for(entered.wait(), TIMEOUT_S)
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), TIMEOUT_S)
        await asyncio.sleep(0.2)
        assert other_tasks() == []


class TestHittingTheDeadline:
    async def test_leaves_no_tasks_behind(self) -> None:
        never = asyncio.Event()

        async def hang() -> None:
            await never.wait()

        team = Team(
            [Turn(tool_calls=[call(LOGS), call(IMPACT)]), synthesis()],
            {LOGS: says("logs ok"), IMPACT: says("never")},
        )
        team.worker_models[IMPACT].before_turn = hang

        limits = RunLimits(timeout_s=0.1, cancel_grace_s=0.1)
        events = await asyncio.wait_for(run(team, limits=limits), TIMEOUT_S)
        assert report_from(events).synthesis_source == "fallback"
        await asyncio.sleep(0.2)
        assert other_tasks() == []

    async def test_a_consumer_slower_than_the_deadline_still_gets_the_report(self) -> None:
        # The deadline must never fire inside the consumer's own code.
        limits = RunLimits(timeout_s=0.05)
        team = Team([synthesis()])
        events: list[RunEvent] = []
        async for event in run_incident(incident(), team, limits=limits):
            events.append(event)
            await asyncio.sleep(0.02)
        assert report_from(events).synthesis_source == "orchestrator"


class TestFinishingNormally:
    async def test_leaves_no_tasks_behind(self) -> None:
        await asyncio.wait_for(run(MidFlight().team), TIMEOUT_S)
        await asyncio.sleep(0.2)
        assert other_tasks() == []
