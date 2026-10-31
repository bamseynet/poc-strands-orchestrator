"""A fake LLM that speaks the Strands `Model` interface and replays scripted turns.

Because it sits behind the real interface, everything above it runs for real: the Strands event
loop, tool execution, concurrency and structured output. Only the model's decisions are scripted.
"""

import asyncio
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any, TypeVar, override
from uuid import uuid4

from pydantic import BaseModel
from strands.models import Model
from strands.types.content import Messages
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolChoice, ToolSpec

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class ToolCall:
    name: str
    input: dict[str, Any]
    tool_use_id: str = field(default_factory=lambda: f"tool-{uuid4().hex[:8]}")


@dataclass(frozen=True)
class Turn:
    """One model response. `usage` is (input, output) tokens, estimated from text when omitted."""

    text: str = ""
    tool_calls: Sequence[ToolCall] = ()
    stop_reason: str | None = None
    usage: tuple[int, int] | None = None

    def with_usage(self, input_tokens: int, output_tokens: int) -> "Turn":
        return replace(self, usage=(input_tokens, output_tokens))


@dataclass(frozen=True)
class ScriptedRequest:
    """What the model was sent on one call, for tests to inspect."""

    messages: Messages
    tool_specs: list[ToolSpec] | None
    system_prompt: str | None
    tool_choice: ToolChoice | None


TurnOutcome = Turn | Exception
TurnScript = Iterable[TurnOutcome] | Callable[[ScriptedRequest], TurnOutcome]


class ScriptExhaustedError(AssertionError):
    """The model was called more times than the script allows for."""


class ScriptedModel(Model):
    """Replays a list of turns, or asks a function for each turn. Raises scripted exceptions."""

    def __init__(
        self,
        script: TurnScript,
        *,
        model_id: str = "scripted",
        chunk_delay_s: float = 0.0,
        before_turn: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._decide = script if callable(script) else None
        self._turns = iter(()) if callable(script) else iter(script)
        self._model_id = model_id
        self._chunk_delay_s = chunk_delay_s
        self.before_turn = before_turn
        self.requests: list[ScriptedRequest] = []

    @override
    def update_config(self, **model_config: Any) -> None:
        return None

    @override
    def get_config(self) -> dict[str, Any]:
        return {"model_id": self._model_id}

    @override
    async def structured_output(
        self,
        output_model: type[T],
        prompt: Messages,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, T | Any], None]:
        raise NotImplementedError("Agents use tool-based structured output with this model")
        yield {}  # pragma: no cover

    def _next_outcome(self, request: ScriptedRequest) -> TurnOutcome:
        if self._decide is not None:
            return self._decide(request)
        try:
            return next(self._turns)
        except StopIteration:
            raise ScriptExhaustedError(
                f"Unscripted model call #{len(self.requests)} on {self._model_id}"
            ) from None

    @override
    async def stream(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        *,
        tool_choice: ToolChoice | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[StreamEvent, None]:
        request = ScriptedRequest(deepcopy(messages), tool_specs, system_prompt, tool_choice)
        self.requests.append(request)
        if self.before_turn is not None:
            await self.before_turn()
        outcome = self._next_outcome(request)
        if isinstance(outcome, Exception):
            raise outcome

        for chunk in _chunks(outcome, request):
            await asyncio.sleep(self._chunk_delay_s)  # always yield, so parallel workers interleave
            yield chunk


def _chunks(turn: Turn, request: ScriptedRequest) -> list[StreamEvent]:
    """Render a turn as the same stream of events the Bedrock provider emits."""
    chunks: list[dict[str, Any]] = [{"messageStart": {"role": "assistant"}}]
    if turn.text:
        chunks.append({"contentBlockStart": {"start": {}}})
        # Split into words, keeping the spaces, so streaming UIs have something to stream.
        chunks.extend(
            {"contentBlockDelta": {"delta": {"text": word}}}
            for word in turn.text.replace(" ", " \0").split("\0")
        )
        chunks.append({"contentBlockStop": {}})
    for call in turn.tool_calls:
        start = {"toolUse": {"name": call.name, "toolUseId": call.tool_use_id}}
        chunks.append({"contentBlockStart": {"start": start}})
        delta = {"toolUse": {"input": json.dumps(call.input)}}
        chunks.append({"contentBlockDelta": {"delta": delta}})
        chunks.append({"contentBlockStop": {}})
    stop_reason = turn.stop_reason or ("tool_use" if turn.tool_calls else "end_turn")
    chunks.append({"messageStop": {"stopReason": stop_reason}})

    input_tokens, output_tokens = turn.usage or _estimate_usage(turn, request)
    usage = {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "totalTokens": input_tokens + output_tokens,
    }
    chunks.append({"metadata": {"usage": usage, "metrics": {"latencyMs": 0}}})
    return chunks  # type: ignore[return-value]


def _estimate_usage(turn: Turn, request: ScriptedRequest) -> tuple[int, int]:
    """Roughly four characters to a token, so simulated runs show plausible numbers."""
    sent = len(json.dumps(request.messages, default=str)) + len(request.system_prompt or "")
    produced = len(turn.text) + sum(len(json.dumps(call.input)) for call in turn.tool_calls)
    return sent // 4, produced // 4
