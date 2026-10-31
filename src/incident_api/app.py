"""The HTTP API: incident in, server-sent events out.

A run is one POST whose response is the event stream, so the API holds no state between requests and
can sit behind a scale-to-zero host. The price is that a dropped connection cannot resume.
"""

import hmac
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

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

    app = FastAPI(title="Incident orchestrator")

    def require_token(x_access_token: Annotated[str | None, Header()] = None) -> None:
        if api.api_access_token is None:
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
            can_simulate_failures=agent.llm_mode == "fake",
            auth_required=api.api_access_token is not None,
            orchestrator_model=agent.bedrock_model_id,
            worker_model=agent.bedrock_worker_model_id,
            workers=[
                WorkerInfo(name=spec.name, description=spec.description) for spec in WORKER_SPECS
            ],
        )

    @app.post("/api/runs", dependencies=[Depends(require_token)])
    async def create_run(request: RunRequest) -> StreamingResponse:
        if request.simulate_failures and agent.llm_mode != "fake":
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "Simulated failures need LLM_MODE=fake."
            )
        slots.acquire()

        async def frames() -> AsyncIterator[str]:
            try:
                events = run_backend.stream(request.incident, request.simulate_failures)
                async for frame in sse_stream(events, api.sse_heartbeat_s):
                    yield frame
            finally:
                slots.release()

        return StreamingResponse(frames(), media_type="text/event-stream", headers=_SSE_HEADERS)

    return app
