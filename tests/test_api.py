"""The HTTP API: validation before streaming, then a run as server-sent events."""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx
import pytest

from incident_agent.config import Settings
from incident_agent.schemas import Incident, RunEvent, RunStarted
from incident_api.app import create_app
from incident_api.backends import InProcessBackend
from incident_api.settings import ApiSettings
from incident_api.sse import sse_stream
from tests.helpers import LOGS

INCIDENT = {
    "title": "reporting-batch export failing",
    "severity": "sev3",
    "service": "reporting-batch",
    "description": "The export job is failing with disk errors.",
}


def fake_agent_settings() -> Settings:
    return Settings(_env_file=None, llm_mode="fake", fake_llm_delay_scale=0)


def client_for(api_settings: ApiSettings | None = None, **kwargs: Any) -> httpx.AsyncClient:
    agent_settings = kwargs.pop("agent_settings", fake_agent_settings())
    app = create_app(
        agent_settings=agent_settings,
        api_settings=api_settings or ApiSettings(_env_file=None),
        **kwargs,
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    frames = []
    for frame in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in frame.splitlines() if not line.startswith(":"))
        frames.append((lines["event"], json.loads(lines["data"])))
    return frames


class TestReadEndpoints:
    async def test_health(self) -> None:
        async with client_for() as client:
            response = await client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    async def test_samples_are_valid_incidents(self) -> None:
        async with client_for() as client:
            samples = (await client.get("/api/samples")).json()
        assert {s["id"] for s in samples} >= {"checkout-5xx", "batch-disk-full"}
        for sample in samples:
            Incident.model_validate(sample)

    async def test_config_tells_the_ui_what_is_available(self) -> None:
        async with client_for() as client:
            config = (await client.get("/api/config")).json()
        assert config["llm_mode"] == "fake"
        assert config["can_simulate_failures"] is True
        assert config["auth_required"] is False
        assert config["orchestrator_model"] == "us.anthropic.claude-opus-5-5"
        assert config["worker_model"] == "us.anthropic.claude-haiku-5-5"
        assert LOGS in {worker["name"] for worker in config["workers"]}

    async def test_config_with_real_bedrock_offers_no_failure_simulation(self) -> None:
        agent_settings = Settings(_env_file=None, llm_mode="bedrock")
        async with client_for(agent_settings=agent_settings) as client:
            config = (await client.get("/api/config")).json()
        assert config["can_simulate_failures"] is False

    async def test_config_never_exposes_the_access_token(self) -> None:
        settings = ApiSettings(_env_file=None, api_access_token="s3cret-value")
        async with client_for(settings) as client:
            response = await client.get("/api/config")
        assert response.json()["auth_required"] is True
        assert "s3cret-value" not in response.text


class TestRunStreaming:
    async def test_streams_a_whole_run_as_named_events(self) -> None:
        async with client_for() as client:
            response = await client.post("/api/runs", json={"incident": INCIDENT})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        frames = parse_sse(response.text)
        kinds = [kind for kind, _ in frames]
        assert kinds[0] == "run_started"
        assert "plan_recorded" in kinds
        assert "worker_finished" in kinds
        assert kinds[-1] == "report_ready"
        assert frames[-1][1]["report"]["status"] == "complete"

    async def test_tells_proxies_not_to_buffer_the_stream(self) -> None:
        async with client_for() as client:
            response = await client.post("/api/runs", json={"incident": INCIDENT})
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"

    async def test_can_simulate_a_worker_failure_in_fake_mode(self) -> None:
        body = {"incident": INCIDENT, "simulate_failures": [LOGS]}
        async with client_for() as client:
            response = await client.post("/api/runs", json=body)
        report = parse_sse(response.text)[-1][1]["report"]
        assert report["status"] == "degraded"

    async def test_a_backend_that_breaks_mid_run_ends_with_a_failure_event(self) -> None:
        class Broken:
            async def stream(
                self, incident: Incident, simulate_failures: Sequence[str]
            ) -> AsyncIterator[RunEvent]:
                yield RunStarted(
                    run_id="r",
                    incident=incident,
                    orchestrator_model="o",
                    worker_model="w",
                    available_workers=[],
                )
                raise RuntimeError("arn:aws:iam::123456789012:role/x exploded")

        async with client_for(backend=Broken()) as client:
            response = await client.post("/api/runs", json={"incident": INCIDENT})
        frames = parse_sse(response.text)
        assert frames[-1][0] == "run_failed"
        assert "123456789012" not in response.text


class TestValidation:
    @pytest.mark.parametrize(
        "incident",
        [
            {**INCIDENT, "severity": "sev9"},
            {**INCIDENT, "title": ""},
            {**INCIDENT, "description": "x" * 4001},
            {"title": "missing fields"},
        ],
    )
    async def test_a_bad_incident_is_rejected_before_any_stream_starts(
        self, incident: dict[str, Any]
    ) -> None:
        async with client_for() as client:
            response = await client.post("/api/runs", json={"incident": incident})
        assert response.status_code == 422
        assert response.headers["content-type"].startswith("application/json")

    async def test_unknown_fields_are_rejected(self) -> None:
        body = {"incident": INCIDENT, "unexpected": True}
        async with client_for() as client:
            assert (await client.post("/api/runs", json=body)).status_code == 422

    async def test_simulating_an_unknown_worker_is_rejected(self) -> None:
        body = {"incident": INCIDENT, "simulate_failures": ["not_a_worker"]}
        async with client_for() as client:
            assert (await client.post("/api/runs", json=body)).status_code == 422

    async def test_failure_simulation_is_refused_with_real_bedrock(self) -> None:
        body = {"incident": INCIDENT, "simulate_failures": [LOGS]}
        agent_settings = Settings(_env_file=None, llm_mode="bedrock")
        async with client_for(agent_settings=agent_settings) as client:
            assert (await client.post("/api/runs", json=body)).status_code == 400


class TestAccessControl:
    @pytest.fixture
    def protected(self) -> ApiSettings:
        return ApiSettings(_env_file=None, api_access_token="correct-token")

    async def test_runs_need_the_token_when_one_is_configured(self, protected: ApiSettings) -> None:
        async with client_for(protected) as client:
            response = await client.post("/api/runs", json={"incident": INCIDENT})
        assert response.status_code == 401

    async def test_a_wrong_token_is_refused(self, protected: ApiSettings) -> None:
        headers = {"Authorization": "Bearer wrong-token"}
        async with client_for(protected) as client:
            response = await client.post("/api/runs", json={"incident": INCIDENT}, headers=headers)
        assert response.status_code == 401

    async def test_the_right_token_is_accepted(self, protected: ApiSettings) -> None:
        headers = {"Authorization": "Bearer correct-token"}
        async with client_for(protected) as client:
            response = await client.post("/api/runs", json={"incident": INCIDENT}, headers=headers)
        assert response.status_code == 200

    async def test_a_non_ascii_token_is_refused_not_a_server_error(
        self, protected: ApiSettings
    ) -> None:
        headers = {"Authorization": "Bearer café".encode()}
        async with client_for(protected) as client:
            response = await client.post("/api/runs", json={"incident": INCIDENT}, headers=headers)
        assert response.status_code == 401


class Blocking:
    """A backend whose runs stay open until released, recording whether they were cancelled."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.closed = False

    async def stream(
        self, incident: Incident, simulate_failures: Sequence[str]
    ) -> AsyncIterator[RunEvent]:
        try:
            yield RunStarted(
                run_id="r",
                incident=incident,
                orchestrator_model="o",
                worker_model="w",
                available_workers=[],
            )
            self.started.set()
            await self.release.wait()
        finally:
            self.closed = True


class TestConcurrencyCap:
    async def test_runs_over_the_cap_are_refused_then_capacity_returns(self) -> None:
        blocking = Blocking()
        settings = ApiSettings(_env_file=None, max_concurrent_runs=1)
        async with client_for(settings, backend=blocking) as client:
            first = asyncio.create_task(client.post("/api/runs", json={"incident": INCIDENT}))
            await asyncio.wait_for(blocking.started.wait(), 2)

            second = await client.post("/api/runs", json={"incident": INCIDENT})
            assert second.status_code == 429

            blocking.release.set()
            assert (await first).status_code == 200

            blocking.started.clear()
            third = await client.post("/api/runs", json={"incident": INCIDENT})
            assert third.status_code == 200


class TestSseStream:
    async def test_sends_heartbeats_while_the_agent_is_quiet(self) -> None:
        blocking = Blocking()
        incident = Incident.model_validate(INCIDENT)
        stream = sse_stream(blocking.stream(incident, []), heartbeat_s=0.01)

        first = await anext(stream)
        assert first.startswith("event: run_started\n")
        assert await anext(stream) == ": heartbeat\n\n"
        assert await anext(stream) == ": heartbeat\n\n"
        await stream.aclose()

    async def test_closing_the_stream_stops_the_run(self) -> None:
        # What happens when a browser navigates away: the agent must not keep spending money.
        blocking = Blocking()
        incident = Incident.model_validate(INCIDENT)
        stream = sse_stream(blocking.stream(incident, []), heartbeat_s=10)

        await anext(stream)
        await asyncio.wait_for(blocking.started.wait(), 2)
        await stream.aclose()
        assert blocking.closed is True

    async def test_closing_mid_run_cancels_real_workers(self) -> None:
        backend = InProcessBackend(
            Settings(_env_file=None, llm_mode="fake", fake_llm_delay_scale=1)
        )
        incident = Incident.model_validate(INCIDENT)
        stream = sse_stream(backend.stream(incident, []), heartbeat_s=10)
        async for frame in stream:
            if frame.startswith("event: worker_started"):
                break
        await asyncio.wait_for(stream.aclose(), 5)
        await asyncio.sleep(0.2)
        leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        assert leftover == [], "no worker task may outlive the client"
