"""The HTTP API: incident in, server-sent events out.

A run is one POST whose response is the event stream, so the API holds no state between requests and
can sit behind a scale-to-zero host. The price is that a dropped connection cannot resume.
"""

import hmac
from collections.abc import AsyncGenerator, Callable
from typing import Annotated, Any, Literal, cast

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import Receive, Scope, Send

from incident_agent import knowledge
from incident_agent.config import Settings
from incident_agent.schemas import Incident
from incident_agent.workers import WORKER_SPECS
from incident_api.backends import AgentBackend, InProcessBackend
from incident_api.settings import ApiSettings
from incident_api.sse import sse_stream

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    # Stops reverse proxies from holding events back until the run ends.
    "X-Accel-Buffering": "no",
}

WorkerName = Literal["log_analysis", "runbook_lookup", "impact_assessment", "comms_drafter"]


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident: Incident
    simulate_failures: list[WorkerName] = Field(default_factory=list, max_length=4)


class WorkerInfo(BaseModel):
    name: str
    description: str


class ClientConfig(BaseModel):
    llm_mode: Literal["fake", "bedrock"]
    can_simulate_failures: bool
    auth_required: bool
    orchestrator_model: str
    worker_model: str
    workers: list[WorkerInfo]


class _RunSlots:
    """A non-blocking cap on concurrent runs. Over the cap, callers get a 429 and do not queue."""

    def __init__(self, limit: int) -> None:
        self._free = limit

    def acquire(self) -> None:
        if self._free == 0:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many runs in progress.")
        self._free -= 1

    def release(self) -> None:
        self._free += 1


class _RunResponse(StreamingResponse):
    """Frees the run's slot however the response ends.

    Releasing inside the body generator is not enough: a generator that never started never runs
    its `finally`, so a client gone before the first frame would hold its slot forever.
    """

    def __init__(self, *args: Any, on_finished: Callable[[], None], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._on_finished = on_finished

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Free the slot only once the run has been wound down, so the cap holds in a storm of
            # disconnects.
            try:
                await cast(AsyncGenerator[str, None], self.body_iterator).aclose()
            finally:
                self._on_finished()


def create_app(
    *,
    agent_settings: Settings | None = None,
    api_settings: ApiSettings | None = None,
    backend: AgentBackend | None = None,
) -> FastAPI:
    agent = agent_settings or Settings()
    api = api_settings or ApiSettings()
    run_backend = backend or InProcessBackend(agent)
    slots = _RunSlots(api.max_concurrent_runs)
    # Failures are simulated by the in-process fake model; a remote agent cannot be told to.
    can_simulate_failures = agent.llm_mode == "fake" and api.agent_backend == "inprocess"

    app = FastAPI(title="Incident orchestrator")

    def require_token(x_access_token: Annotated[str | None, Header()] = None) -> None:
        if not api.api_access_token:
            return
        # Not Authorization: CloudFront's origin access control overwrites that with its signature.
        # Comparing bytes keeps a non-ASCII token an ordinary refusal rather than a TypeError.
        supplied = (x_access_token or "").encode()
        if not hmac.compare_digest(supplied, api.api_access_token.encode()):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "A valid access token is required.")

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/samples")
    def samples() -> list[dict[str, Any]]:
        return knowledge.sample_incidents()

    @app.get("/api/config")
    def config() -> ClientConfig:
        return ClientConfig(
            llm_mode=agent.llm_mode,
            can_simulate_failures=can_simulate_failures,
            auth_required=bool(api.api_access_token),
            orchestrator_model=agent.bedrock_model_id,
            worker_model=agent.bedrock_worker_model_id,
            workers=[
                WorkerInfo(name=spec.name, description=spec.description) for spec in WORKER_SPECS
            ],
        )

    @app.post("/api/runs", dependencies=[Depends(require_token)])
    async def create_run(request: RunRequest) -> StreamingResponse:
        if request.simulate_failures and not can_simulate_failures:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "Simulated failures need LLM_MODE=fake."
            )
        slots.acquire()
        try:
            events = run_backend.stream(request.incident, request.simulate_failures)
            return _RunResponse(
                sse_stream(events, api.sse_heartbeat_s),
                on_finished=slots.release,
                media_type="text/event-stream",
                headers=_SSE_HEADERS,
            )
        except BaseException:
            # Building the models can fail (credentials, region) before any response owns the slot.
            slots.release()
            raise

    return app
