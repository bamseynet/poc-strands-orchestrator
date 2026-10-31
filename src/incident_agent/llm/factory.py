"""Chooses the LLM back end. The agents are identical in both modes."""

from collections.abc import Collection

from botocore.config import Config

from incident_agent.config import Settings
from incident_agent.llm.base import ModelSet
from incident_agent.llm.bedrock import AbortableBedrockModel
from incident_agent.llm.demo import build_demo_models
from incident_agent.workers import WORKER_NAMES

# Thinking counts towards max_tokens on these models, so leave room for it as well as the answer.
_ORCHESTRATOR_MAX_TOKENS = 32_000
_WORKER_MAX_TOKENS = 16_000
# A hung connection must surface as a failure the orchestrator can work around.
_BOTO_CONFIG = Config(
    connect_timeout=10, read_timeout=120, retries={"max_attempts": 3, "mode": "standard"}
)


def build_models(settings: Settings, simulate_failures: Collection[str] = ()) -> ModelSet:
    """Build the model tiers. `simulate_failures` names workers to break, and is fake-mode only."""
    if settings.llm_mode == "fake":
        return build_demo_models(
            orchestrator_model_id=settings.bedrock_model_id,
            worker_model_id=settings.bedrock_worker_model_id,
            delay_scale=settings.fake_llm_delay_scale,
            simulate_failures=simulate_failures,
        )
    if simulate_failures:
        raise ValueError("simulate_failures is only available with LLM_MODE=fake")

    # No temperature, top_p or top_k: Opus 5.5 rejects them. Effort is left at the model default.
    orchestrator = AbortableBedrockModel(
        model_id=settings.bedrock_model_id,
        region_name=settings.aws_region,
        max_tokens=_ORCHESTRATOR_MAX_TOKENS,
        boto_client_config=_BOTO_CONFIG,
    )
    worker = AbortableBedrockModel(
        model_id=settings.bedrock_worker_model_id,
        region_name=settings.aws_region,
        max_tokens=_WORKER_MAX_TOKENS,
        boto_client_config=_BOTO_CONFIG,
    )
    return ModelSet(
        orchestrator=orchestrator,
        workers=dict.fromkeys(WORKER_NAMES, worker),
        orchestrator_model_id=settings.bedrock_model_id,
        worker_model_id=settings.bedrock_worker_model_id,
    )
