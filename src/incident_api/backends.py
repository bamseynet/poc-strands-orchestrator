"""Where the agent runs. The API only needs a stream of events, wherever they come from."""

import asyncio
import codecs
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Iterator, Sequence
from typing import Any, Protocol
from uuid import uuid4

import boto3
from botocore.config import Config
from pydantic import ValidationError

from incident_agent.config import Settings
from incident_agent.llm.factory import build_models
from incident_agent.redaction import redact
from incident_agent.runner import RunLimits, run_incident
from incident_agent.schemas import RUN_EVENT_ADAPTER, Incident, ReportReady, RunEvent, RunFailed
from incident_api.settings import ApiSettings

logger = logging.getLogger(__name__)

# The agent can think for minutes between events. Retrying would start a second, billable run.
# The read timeout sits just above the agent's own 300s run deadline (and below the API Lambda's
# 6 minutes), so the agent's explanation of a timeout arrives before the transport gives up.
_AGENTCORE_CLIENT_CONFIG = Config(
    connect_timeout=10, read_timeout=330, retries={"total_max_attempts": 1}
)


class AgentBackend(Protocol):
    def stream(
        self, incident: Incident, simulate_failures: Sequence[str]
    ) -> AsyncIterator[RunEvent]: ...


class InProcessBackend:
    """Runs the agent in this process: what `make dev` uses."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def stream(
        self, incident: Incident, simulate_failures: Sequence[str]
    ) -> AsyncIterator[RunEvent]:
        limits = RunLimits(
            turns=self._settings.max_total_turns,
            total_tokens=self._settings.max_total_tokens,
            timeout_s=self._settings.run_timeout_s,
        )
        models = build_models(self._settings, simulate_failures=simulate_failures)
        return run_incident(incident, models, limits=limits)


class AgentCoreBackend:
    """Calls the agent on Bedrock AgentCore Runtime and relays its events."""

    def __init__(self, runtime_arn: str, client: Any) -> None:
        self._runtime_arn = runtime_arn
        self._client = client

    def stream(
        self, incident: Incident, simulate_failures: Sequence[str]
    ) -> AsyncIterator[RunEvent]:
        if simulate_failures:
            raise ValueError("Simulated failures need LLM_MODE=fake")
        return self._stream(incident)

    async def _stream(self, incident: Incident) -> AsyncGenerator[RunEvent, None]:
        response = await asyncio.to_thread(
            self._client.invoke_agent_runtime,
            agentRuntimeArn=self._runtime_arn,
            runtimeSessionId=str(uuid4()),
            contentType="application/json",
            accept="text/event-stream",
            payload=json.dumps({"incident": incident.model_dump(mode="json")}).encode(),
        )
        body = response["response"]
        # A read blocks until it has chunk_size bytes, and these frames are small and minutes
        # apart, so the default of 1 KiB would hold progress back. One byte returns what is there.
        chunks = body.iter_chunks(chunk_size=1)
        try:
            async for data in _sse_data(chunks):
                event = _parse_frame(data)
                yield event
                if isinstance(event, ReportReady | RunFailed):
                    return
            yield RunFailed(error="The agent's stream ended before the run finished.")
        finally:
            # Dropping the connection is what stops the remote run and its spending.
            body.close()


_FRAME_END = (b"\n\n", b"\r\n\r\n", b"\n\r\n")


def _next_frame_bytes(chunks: Iterator[bytes]) -> bytes | None:
    """Everything up to the end of the next frame, or None at the end of the stream.

    The body is read a byte at a time (see `_stream`), so gathering a whole frame here costs one
    thread hop per frame instead of one per byte.
    """
    gathered = bytearray()
    for chunk in chunks:
        gathered += chunk
        if gathered.endswith(_FRAME_END):
            break
    return bytes(gathered) if gathered else None


async def _sse_data(chunks: Iterator[bytes]) -> AsyncGenerator[str, None]:
    """The `data` of each SSE frame. Reads in a thread, because the body is a blocking stream."""
    # Incremental, because a multi-byte character can be split across two chunks.
    decoder = codecs.getincrementaldecoder("utf-8")()
    buffer = ""
    while (chunk := await asyncio.to_thread(_next_frame_bytes, chunks)) is not None:
        # The SSE spec allows CRLF. The buffer is kept whole, so a CR and LF split apart still join.
        buffer = (buffer + decoder.decode(chunk)).replace("\r\n", "\n")
        while "\n\n" in buffer:
            frame, buffer = buffer.split("\n\n", 1)
            if data := _frame_data(frame):
                yield data
    # A last frame the runtime did not terminate with a blank line is still a frame.
    buffer += decoder.decode(b"", final=True)
    if data := _frame_data(buffer):
        yield data


def _frame_data(frame: str) -> str | None:
    lines = [line[5:].strip() for line in frame.split("\n") if line.startswith("data:")]
    return "\n".join(lines) if lines else None


def _parse_frame(data: str) -> RunEvent:
    """A typed event, or a failure event if the runtime sent anything else."""
    try:
        parsed = json.loads(data)
        if isinstance(parsed, dict) and "type" not in parsed and "error" in parsed:
            return RunFailed(error=redact(f"The agent failed: {parsed['error']}"))
        return RUN_EVENT_ADAPTER.validate_python(parsed)
    except (json.JSONDecodeError, ValidationError):
        logger.exception("Unrecognised frame from the agent runtime")
        return RunFailed(error="The agent sent a response that could not be understood.")


def build_backend(agent_settings: Settings, api_settings: ApiSettings) -> AgentBackend:
    """Run the agent here, or call it on AgentCore, as configured."""
    if api_settings.agent_backend == "inprocess":
        return InProcessBackend(agent_settings)
    if not api_settings.agent_runtime_arn:
        raise ValueError("AGENT_BACKEND=agentcore needs AGENT_RUNTIME_ARN")
    client = boto3.client(
        "bedrock-agentcore", region_name=agent_settings.aws_region, config=_AGENTCORE_CLIENT_CONFIG
    )
    return AgentCoreBackend(api_settings.agent_runtime_arn, client)
