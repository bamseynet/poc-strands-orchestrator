"""Turns a stream of run events into server-sent events, with heartbeats during quiet spells."""

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


async def sse_stream(
    events: AsyncIterator[RunEvent], heartbeat_s: float
) -> AsyncGenerator[str, None]:
    """Yield SSE frames for `events`. Closing this generator stops the run behind it.

    A task pulls the events so that a heartbeat timeout only ever abandons a wait on the queue. A
    timeout on the event stream itself would cancel the run.
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
            try:
                await cast(AsyncGenerator[RunEvent, None], events).aclose()
            finally:
                # Without this, a failing aclose leaves the consumer sending heartbeats forever.
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
