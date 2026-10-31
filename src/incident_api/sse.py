"""Turns a stream of run events into server-sent events, with heartbeats while the agent is quiet."""

import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from typing import cast

from incident_agent.redaction import redact
from incident_agent.schemas import RunEvent, RunFailed

logger = logging.getLogger(__name__)

_HEARTBEAT = ": heartbeat\n\n"
_DONE = object()


def format_event(event: RunEvent) -> str:
    return f"event: {event.type}\ndata: {event.model_dump_json()}\n\n"


async def sse_stream(events: AsyncIterator[RunEvent], heartbeat_s: float) -> AsyncIterator[str]:
    """Yield SSE frames for `events`. Closing this generator stops the run behind it.

    One task owns the event generator from start to finish. Pulling from it directly with a timeout
    would cancel it, and resuming it from a new task for each event would break the OTel context
    managers Strands keeps open inside it.
    """
    queue: asyncio.Queue[object] = asyncio.Queue()

    async def pump() -> None:
        try:
            async for event in events:
                queue.put_nowait(event)
        except Exception as error:
            # The response has already started, so an HTTP error is no longer possible.
            logger.exception("Run failed mid-stream")
            queue.put_nowait(RunFailed(error=redact(f"The run failed: {error}")))
        finally:
            await cast(AsyncGenerator[RunEvent, None], events).aclose()
            queue.put_nowait(_DONE)

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), heartbeat_s)
            except TimeoutError:
                yield _HEARTBEAT
                continue
            if item is _DONE:
                return
            yield format_event(cast(RunEvent, item))
    finally:
        pump_task.cancel()
        await asyncio.gather(pump_task, return_exceptions=True)
