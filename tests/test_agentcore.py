"""Running the agent on AgentCore Runtime, and the API calling it there. No AWS involved."""

import asyncio
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from botocore.exceptions import ClientError

from incident_agent import agentcore_app
from incident_agent.config import Settings
from incident_agent.schemas import Incident, ReportReady, RunFailed, RunStarted
from incident_api import backends
from incident_api.backends import AgentCoreBackend, build_backend
from incident_api.settings import ApiSettings, resolve_api_settings

INCIDENT = {
    "title": "reporting-batch export failing",
    "severity": "sev3",
    "service": "reporting-batch",
    "description": "The export job is failing with disk errors.",
}
RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/incident-abc"


def run_started_json() -> dict[str, Any]:
    return RunStarted(
        run_id="r",
        incident=Incident.model_validate(INCIDENT),
        orchestrator_model="o",
        worker_model="w",
        available_workers=[],
    ).model_dump(mode="json")


class TestAgentCoreEntrypoint:
    """The service contract: POST /invocations and GET /ping on the SDK's app."""

    @pytest.fixture
    def client(self, monkeypatch: pytest.MonkeyPatch) -> httpx.AsyncClient:
        monkeypatch.setenv("LLM_MODE", "fake")
        monkeypatch.setenv("FAKE_LLM_DELAY_SCALE", "0")
        transport = httpx.ASGITransport(app=agentcore_app.app)
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    @staticmethod
    def events_in(body: str) -> list[dict[str, Any]]:
        return [
            json.loads(line.removeprefix("data: "))
            for line in body.splitlines()
            if line.startswith("data: ")
        ]

    async def test_ping_reports_healthy(self, client: httpx.AsyncClient) -> None:
        async with client:
            response = await client.get("/ping")
        assert response.status_code == 200
        assert response.json()["status"] in {"Healthy", "HealthyBusy"}

    async def test_invocation_streams_a_whole_run_as_sse(self, client: httpx.AsyncClient) -> None:
        async with client:
            response = await client.post("/invocations", json={"incident": INCIDENT})
        assert response.headers["content-type"].startswith("text/event-stream")
        events = self.events_in(response.text)
        assert events[0]["type"] == "run_started"
        assert events[-1]["type"] == "report_ready"

    @pytest.mark.parametrize(
        "payload", [{}, {"incident": {"title": "incomplete"}}, {"incident": "not an object"}]
    )
    async def test_a_bad_payload_ends_in_a_failure_event(
        self, client: httpx.AsyncClient, payload: dict[str, Any]
    ) -> None:
        async with client:
            response = await client.post("/invocations", json=payload)
        events = self.events_in(response.text)
        assert [e["type"] for e in events] == ["run_failed"]


class FakeBody:
    """Stands in for botocore's StreamingBody."""

    def __init__(self, chunks: list[bytes], *, then: threading.Event | None = None) -> None:
        self._chunks = chunks
        self._then = then
        self.closed = False

    def iter_chunks(self, chunk_size: int = 1024) -> Iterator[bytes]:
        yield from self._chunks
        if self._then is not None:
            self._then.wait(5)

    def close(self) -> None:
        self.closed = True
        if self._then is not None:
            self._then.set()


class FakeAgentCoreClient:
    def __init__(self, body: FakeBody | Exception) -> None:
        self._body = body
        self.requests: list[dict[str, Any]] = []

    def invoke_agent_runtime(self, **request: Any) -> dict[str, Any]:
        self.requests.append(request)
        if isinstance(self._body, Exception):
            raise self._body
        return {"response": self._body, "contentType": "text/event-stream"}


def backend_with(client: FakeAgentCoreClient) -> AgentCoreBackend:
    return AgentCoreBackend(runtime_arn=RUNTIME_ARN, client=client)


def frame(event: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(event)}\n\n".encode()


async def collect(backend: AgentCoreBackend) -> list[Any]:
    incident = Incident.model_validate(INCIDENT)
    return [event async for event in backend.stream(incident, [])]


class TestAgentCoreBackend:
    async def test_relays_the_agents_events_as_typed_events(self) -> None:
        client = FakeAgentCoreClient(FakeBody([frame(run_started_json())]))
        events = await collect(backend_with(client))
        assert isinstance(events[0], RunStarted)

    async def test_survives_a_frame_split_across_chunks(self) -> None:
        raw = frame(run_started_json())
        client = FakeAgentCoreClient(FakeBody([raw[:20], raw[20:]]))
        assert isinstance((await collect(backend_with(client)))[0], RunStarted)

    async def test_survives_a_multibyte_character_split_across_chunks(self) -> None:
        started = run_started_json()
        started["incident"]["title"] = "café"
        raw = frame(started).replace(b"caf\\u00e9", "café".encode())
        split = raw.index(b"\xc3") + 1
        client = FakeAgentCoreClient(FakeBody([raw[:split], raw[split:]]))
        events = await collect(backend_with(client))
        assert events[0].incident.title == "café"

    async def test_sends_what_the_runtime_api_requires(self) -> None:
        client = FakeAgentCoreClient(FakeBody([]))
        await collect(backend_with(client))
        request = client.requests[0]
        assert request["agentRuntimeArn"] == RUNTIME_ARN
        assert len(request["runtimeSessionId"]) >= 33, "the API rejects anything shorter"
        assert request["accept"] == "text/event-stream"
        assert json.loads(request["payload"])["incident"]["title"] == INCIDENT["title"]

    async def test_every_run_gets_its_own_session(self) -> None:
        client = FakeAgentCoreClient(FakeBody([]))
        await collect(backend_with(client))
        await collect(backend_with(client))
        sessions = {request["runtimeSessionId"] for request in client.requests}
        assert len(sessions) == 2, "a shared session would route runs to one microVM"

    async def test_a_runtime_error_frame_becomes_a_failure_event(self) -> None:
        frames = [frame({"error": "boom", "error_type": "RuntimeError"})]
        events = await collect(backend_with(FakeAgentCoreClient(FakeBody(frames))))
        assert isinstance(events[-1], RunFailed)
        assert "boom" in events[-1].error

    async def test_an_unrecognised_frame_becomes_a_failure_not_a_crash(self) -> None:
        frames = [b"data: not json\n\n"]
        events = await collect(backend_with(FakeAgentCoreClient(FakeBody(frames))))
        assert isinstance(events[-1], RunFailed)

    async def test_a_stream_ending_without_a_result_is_a_failure(self) -> None:
        client = FakeAgentCoreClient(FakeBody([frame(run_started_json())]))
        events = await collect(backend_with(client))
        assert isinstance(events[-1], RunFailed)
        assert "ended" in events[-1].error

    async def test_a_finished_run_gets_no_extra_failure(self) -> None:
        fixture = Path(__file__).parent.parent / "web/src/test/fixtures/run-complete.jsonl"
        report = json.loads(fixture.read_text().splitlines()[-1])
        events = await collect(backend_with(FakeAgentCoreClient(FakeBody([frame(report)]))))
        assert [type(e) for e in events] == [ReportReady]

    async def test_an_aws_error_propagates_for_the_stream_layer_to_report(self) -> None:
        error = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "InvokeAgentRuntime"
        )
        with pytest.raises(ClientError):
            await collect(backend_with(FakeAgentCoreClient(error)))

    async def test_closing_the_stream_closes_the_connection_to_the_runtime(self) -> None:
        # This is what stops the remote run, and its spending, when a browser goes away.
        release = threading.Event()
        body = FakeBody([frame(run_started_json())], then=release)
        incident = Incident.model_validate(INCIDENT)
        stream = backend_with(FakeAgentCoreClient(body)).stream(incident, [])
        assert isinstance(await anext(stream), RunStarted)
        await asyncio.wait_for(stream.aclose(), 2)  # type: ignore[attr-defined]
        assert body.closed is True

    def test_failure_simulation_is_not_sent_to_a_real_deployment(self) -> None:
        incident = Incident.model_validate(INCIDENT)
        with pytest.raises(ValueError, match="fake"):
            backend_with(FakeAgentCoreClient(FakeBody([]))).stream(incident, ["log_analysis"])


class TestBackendSelection:
    def test_defaults_to_in_process(self) -> None:
        backend = build_backend(
            Settings(_env_file=None), ApiSettings(_env_file=None, agent_backend="inprocess")
        )
        assert isinstance(backend, backends.InProcessBackend)

    def test_agentcore_needs_a_runtime_arn(self) -> None:
        with pytest.raises(ValueError, match="AGENT_RUNTIME_ARN"):
            build_backend(
                Settings(_env_file=None), ApiSettings(_env_file=None, agent_backend="agentcore")
            )

    def test_agentcore_uses_the_configured_runtime(self) -> None:
        settings = ApiSettings(
            _env_file=None, agent_backend="agentcore", agent_runtime_arn=RUNTIME_ARN
        )
        assert isinstance(build_backend(Settings(_env_file=None), settings), AgentCoreBackend)


class FakeSecrets:
    def __init__(self, value: str | Exception) -> None:
        self.value = value
        self.requested: list[str] = []

    def get_secret_value(self, SecretId: str) -> dict[str, str]:
        self.requested.append(SecretId)
        if isinstance(self.value, Exception):
            raise self.value
        return {"SecretString": self.value}


SECRET_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:token-AbCdEf"


class TestAccessTokenFromSecretsManager:
    def test_reads_the_token_from_the_referenced_secret(self) -> None:
        secrets = FakeSecrets("from-the-secret")
        settings = ApiSettings(_env_file=None, api_access_token_secret_arn=SECRET_ARN)
        resolved = resolve_api_settings(settings, secrets_client=secrets)
        assert resolved.api_access_token == "from-the-secret"
        assert secrets.requested == [SECRET_ARN]

    def test_refuses_to_start_if_the_secret_cannot_be_read(self) -> None:
        # Failing open here would leave a spending endpoint unprotected.
        secrets = FakeSecrets(RuntimeError("access denied"))
        settings = ApiSettings(_env_file=None, api_access_token_secret_arn=SECRET_ARN)
        with pytest.raises(RuntimeError, match="access denied"):
            resolve_api_settings(settings, secrets_client=secrets)

    def test_refuses_an_empty_secret(self) -> None:
        settings = ApiSettings(_env_file=None, api_access_token_secret_arn=SECRET_ARN)
        with pytest.raises(ValueError, match="empty"):
            resolve_api_settings(settings, secrets_client=FakeSecrets("  "))

    def test_a_deployment_calling_agentcore_must_have_a_token(self) -> None:
        settings = ApiSettings(
            _env_file=None, agent_backend="agentcore", agent_runtime_arn=RUNTIME_ARN
        )
        with pytest.raises(ValueError, match="token"):
            resolve_api_settings(settings)

    def test_local_use_needs_no_secret_or_client(self) -> None:
        settings = ApiSettings(_env_file=None)
        assert resolve_api_settings(settings).api_access_token is None
