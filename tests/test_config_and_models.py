"""Configuration, and the real BedrockModel driven through a stubbed AWS client (no network)."""

import asyncio
import json
from typing import Any

import boto3
import pytest
from pydantic import ValidationError
from strands.models import BedrockModel

from incident_agent.config import Settings
from incident_agent.llm.factory import build_models
from incident_agent.llm.scripted import ScriptedModel
from incident_agent.schemas import IncidentSynthesis
from incident_agent.workers import WORKER_NAMES
from tests.helpers import LOGS, report_from, run

OPUS = "eu.anthropic.claude-opus-5-5"
HAIKU = "eu.anthropic.claude-haiku-5-5"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LLM_MODE", "BEDROCK_MODEL_ID", "BEDROCK_WORKER_MODEL_ID", "AWS_REGION"):
        monkeypatch.delenv(name, raising=False)


class TestSettings:
    def test_defaults_to_the_fake_llm_so_it_runs_without_aws(self) -> None:
        assert Settings(_env_file=None).llm_mode == "fake"

    def test_defaults_are_the_documented_bedrock_inference_profiles(self) -> None:
        settings = Settings(_env_file=None)
        assert settings.bedrock_model_id == "us.anthropic.claude-opus-5-5"
        assert settings.bedrock_worker_model_id == "us.anthropic.claude-haiku-5-5"
        assert settings.aws_region == "us-east-1"

    def test_read_from_the_agreed_environment_variables(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_MODE", "bedrock")
        monkeypatch.setenv("BEDROCK_MODEL_ID", OPUS)
        monkeypatch.setenv("BEDROCK_WORKER_MODEL_ID", HAIKU)
        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        settings = Settings(_env_file=None)
        assert settings.llm_mode == "bedrock"
        assert settings.bedrock_model_id == OPUS
        assert settings.bedrock_worker_model_id == HAIKU
        assert settings.aws_region == "eu-west-1"

    def test_an_unknown_llm_mode_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_MODE", "chatgpt")
        with pytest.raises(ValidationError):
            Settings(_env_file=None)


def bedrock_settings() -> Settings:
    return Settings(
        _env_file=None,
        llm_mode="bedrock",
        bedrock_model_id=OPUS,
        bedrock_worker_model_id=HAIKU,
        aws_region="eu-west-1",
    )


class TestFactory:
    def test_fake_mode_never_creates_an_aws_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def explode(*_: Any, **__: Any) -> None:
            raise AssertionError("fake mode must not touch AWS")

        monkeypatch.setattr(boto3.Session, "client", explode)
        models = build_models(Settings(_env_file=None))
        assert isinstance(models.orchestrator, ScriptedModel)
        assert set(models.workers) == set(WORKER_NAMES)

    def test_bedrock_mode_gives_each_tier_its_own_model_id(self) -> None:
        models = build_models(bedrock_settings())
        assert isinstance(models.orchestrator, BedrockModel)
        assert models.orchestrator.get_config()["model_id"] == OPUS
        for model in models.workers.values():
            assert isinstance(model, BedrockModel)
            assert model.get_config()["model_id"] == HAIKU
        assert models.orchestrator_model_id == OPUS
        assert models.worker_model_id == HAIKU

    def test_bedrock_mode_uses_the_configured_region(self) -> None:
        model = build_models(bedrock_settings()).orchestrator
        assert isinstance(model, BedrockModel)
        assert model.client.meta.region_name == "eu-west-1"

    def test_bedrock_requests_send_no_sampling_parameters(self) -> None:
        # Opus 5.5 rejects temperature, top_p and top_k with a 400.
        model = build_models(bedrock_settings()).orchestrator
        assert isinstance(model, BedrockModel)
        config = model.get_config()
        assert not {"temperature", "top_p", "top_k"} & {k for k, v in config.items() if v}

    def test_simulated_failures_are_refused_with_real_bedrock(self) -> None:
        with pytest.raises(ValueError, match="fake"):
            build_models(bedrock_settings(), simulate_failures=[LOGS])


def _stream(*blocks: dict[str, Any], stop: str = "end_turn") -> dict[str, Any]:
    """Shape a Converse stream response the way bedrock-runtime returns it."""
    events: list[dict[str, Any]] = [{"messageStart": {"role": "assistant"}}]
    for index, block in enumerate(blocks):
        if "toolUse" in block:
            start = {"toolUse": {"name": block["toolUse"], "toolUseId": block["id"]}}
            delta = {"toolUse": {"input": json.dumps(block["input"])}}
            events.append({"contentBlockStart": {"start": start, "contentBlockIndex": index}})
        else:
            delta = {"text": block["text"]}
        events.append({"contentBlockDelta": {"delta": delta, "contentBlockIndex": index}})
        events.append({"contentBlockStop": {"contentBlockIndex": index}})
    events.append({"messageStop": {"stopReason": stop}})
    usage = {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}
    events.append({"metadata": {"usage": usage, "metrics": {"latencyMs": 1}}})
    return {"stream": iter(events)}


SYNTHESIS_INPUT = {
    "summary": "s",
    "probable_cause": "c",
    "confidence": "low",
    "recommended_actions": [],
    "worker_contributions": [],
}


def _synthesis_call() -> dict[str, Any]:
    return _stream(
        {"toolUse": IncidentSynthesis.__name__, "id": "syn", "input": SYNTHESIS_INPUT},
        stop="tool_use",
    )


class FakeBedrock:
    """Stands in for the bedrock-runtime client's converse_stream, which runs off the event loop."""

    def __init__(self, respond: Any) -> None:
        self.respond = respond
        self.calls: list[dict[str, Any]] = []

    def converse_stream(self, **request: Any) -> Any:
        self.calls.append(request)
        return self.respond(request, len(self.calls))


class TestRealProviderAgainstStubbedBedrock:
    def _wire(self, models: Any, fake: FakeBedrock, monkeypatch: pytest.MonkeyPatch) -> None:
        clients = {models.orchestrator.client, *(m.client for m in models.workers.values())}
        for client in clients:
            monkeypatch.setattr(client, "converse_stream", fake.converse_stream)

    async def test_orchestrator_and_workers_call_their_own_models(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def respond(request: dict[str, Any], _: int) -> dict[str, Any]:
            if request["modelId"] == HAIKU:
                return _stream({"text": "worker finding"})
            if not any("toolResult" in str(m) for m in request["messages"]):
                return _stream(
                    {"toolUse": LOGS, "id": "c1", "input": {"input": "look"}}, stop="tool_use"
                )
            return _synthesis_call()

        fake = FakeBedrock(respond)
        models = build_models(bedrock_settings())
        self._wire(models, fake, monkeypatch)

        report = report_from(await asyncio.wait_for(run(models), 10))

        assert report.worker_findings[0].output == "worker finding"
        assert {call["modelId"] for call in fake.calls} == {OPUS, HAIKU}
        assert fake.calls[0]["modelId"] == OPUS

    async def test_first_request_lets_the_model_choose_its_tools(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeBedrock(lambda *_: _synthesis_call())
        models = build_models(bedrock_settings())
        self._wire(models, fake, monkeypatch)

        await asyncio.wait_for(run(models), 10)

        tool_config = fake.calls[0]["toolConfig"]
        assert tool_config["toolChoice"] == {"auto": {}}
        offered = {tool["toolSpec"]["name"] for tool in tool_config["tools"]}
        assert offered >= set(WORKER_NAMES)
        inference = fake.calls[0].get("inferenceConfig", {})
        assert not {"temperature", "topP"} & set(inference)

    async def test_opus_55_prose_is_retried_for_json_without_forcing_a_tool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Opus 5.5 returns a 400 for tool_choice any/tool. Strands knows, so when the model answers
        # in prose it retries with native structured output and auto tool choice instead.
        payload = {**SYNTHESIS_INPUT, "summary": "from json"}

        def respond(request: dict[str, Any], call_number: int) -> dict[str, Any]:
            if call_number == 1:
                return _stream({"text": "Here is my answer in prose."})
            assert not set(request["toolConfig"]["toolChoice"]) & {"any", "tool"}
            assert "textFormat" in request["outputConfig"]
            return _stream({"text": json.dumps(payload)})

        fake = FakeBedrock(respond)
        settings = bedrock_settings().model_copy(
            update={"bedrock_model_id": "us.anthropic.claude-opus-5-5"}
        )
        models = build_models(settings)
        self._wire(models, fake, monkeypatch)

        report = report_from(await asyncio.wait_for(run(models), 10))

        assert len(fake.calls) == 2
        assert report.synthesis_source == "orchestrator"
        assert report.summary == "from json"
