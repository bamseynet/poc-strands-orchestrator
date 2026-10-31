"""OTel wiring: spans for the orchestrator and each worker, joined up in one trace."""

from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from strands.telemetry import tracer as strands_tracer

from incident_agent import telemetry
from incident_agent.config import Settings
from incident_agent.llm.scripted import Turn
from tests.helpers import IMPACT, LOGS, Team, call, incident, run, says, synthesis


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    monkeypatch.setattr(trace_api, "get_tracer_provider", lambda: provider)
    monkeypatch.setattr(strands_tracer, "_tracer_instance", None)
    yield memory
    provider.shutdown()


class TestSpans:
    async def _run_team(self) -> None:
        team = Team(
            [Turn(tool_calls=[call(LOGS), call(IMPACT)]), synthesis([LOGS, IMPACT])],
            {LOGS: says("a"), IMPACT: says("b")},
        )
        await run(team, incident(id="inc-traced"))

    async def test_orchestrator_and_each_worker_get_their_own_span(
        self, exporter: InMemorySpanExporter
    ) -> None:
        await self._run_team()
        names = {span.name for span in exporter.get_finished_spans()}
        for agent in ("incident_commander", LOGS, IMPACT):
            assert f"invoke_agent {agent}" in names

    async def test_workers_are_nested_in_the_orchestrators_trace(
        self, exporter: InMemorySpanExporter
    ) -> None:
        await self._run_team()
        spans = exporter.get_finished_spans()
        assert len({span.context.trace_id for span in spans}) == 1
        worker = next(span for span in spans if span.name == f"invoke_agent {LOGS}")
        assert worker.parent is not None

    async def test_spans_carry_the_incident_id(self, exporter: InMemorySpanExporter) -> None:
        await self._run_team()
        agent_spans = [
            s for s in exporter.get_finished_spans() if s.name.startswith("invoke_agent")
        ]
        assert agent_spans
        for span in agent_spans:
            assert (span.attributes or {}).get("incident.id") == "inc-traced"


class FakeStrandsTelemetry:
    instances: list["FakeStrandsTelemetry"] = []  # noqa: RUF012

    def __init__(self) -> None:
        self.exporters: list[str] = []
        FakeStrandsTelemetry.instances.append(self)

    def setup_otlp_exporter(self, **_: Any) -> "FakeStrandsTelemetry":
        self.exporters.append("otlp")
        return self

    def setup_console_exporter(self, **_: Any) -> "FakeStrandsTelemetry":
        self.exporters.append("console")
        return self


class TestConfigure:
    @pytest.fixture(autouse=True)
    def fake_telemetry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        FakeStrandsTelemetry.instances = []
        monkeypatch.setattr(telemetry, "StrandsTelemetry", FakeStrandsTelemetry)
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        monkeypatch.setattr(
            trace_api, "get_tracer_provider", lambda: trace_api.ProxyTracerProvider()
        )

    def test_does_nothing_unless_asked(self) -> None:
        assert telemetry.configure_telemetry(Settings(_env_file=None)) is False
        assert FakeStrandsTelemetry.instances == []

    def test_exports_over_otlp_when_an_endpoint_is_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
        assert telemetry.configure_telemetry(Settings(_env_file=None)) is True
        assert FakeStrandsTelemetry.instances[0].exporters == ["otlp"]

    def test_console_export_can_be_switched_on_for_local_debugging(self) -> None:
        settings = Settings(_env_file=None, otel_console_export=True)
        assert telemetry.configure_telemetry(settings) is True
        assert FakeStrandsTelemetry.instances[0].exporters == ["console"]

    def test_leaves_an_existing_provider_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # On AgentCore, `opentelemetry-instrument` (ADOT) has already installed one.
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
        monkeypatch.setattr(trace_api, "get_tracer_provider", TracerProvider)
        assert telemetry.configure_telemetry(Settings(_env_file=None)) is False
        assert FakeStrandsTelemetry.instances == []
