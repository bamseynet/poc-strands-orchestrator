"""The Bedrock model, adjusted so that a stopped run stops generating (and billing)."""

import asyncio
import threading
from collections.abc import AsyncGenerator
from typing import Any, override

from strands.models import BedrockModel
from strands.types.content import Messages
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolSpec

_POLL_S = 0.02


class AbortableBedrockModel(BedrockModel):
    """Keeps a cancelled stream cancelled until the thread reading from Bedrock has seen it.

    Strands' reader thread checks the cancel signal between chunks, but a cancelled agent clears
    that signal as it unwinds, usually before the next chunk arrives. The thread then reads the
    response to its end, with the tokens billed but never counted. Reading through an event that is
    never cleared closes the stream at the next chunk. A read blocked with no chunks at all is still
    only bounded by the client's read timeout.
    """

    @override
    async def stream(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        *,
        cancel_signal: threading.Event | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[StreamEvent, None]:
        stop = threading.Event()

        async def mirror_cancellation() -> None:
            # A threading.Event has no async wait, and Strands polls these the same way.
            while cancel_signal is None or not cancel_signal.is_set():  # noqa: ASYNC110
                await asyncio.sleep(_POLL_S)
            stop.set()

        mirror = asyncio.create_task(mirror_cancellation())
        try:
            async for event in super().stream(
                messages, tool_specs, system_prompt, cancel_signal=stop, **kwargs
            ):
                yield event
        finally:
            stop.set()
            mirror.cancel()
