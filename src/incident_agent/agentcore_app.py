"""The agent as an AgentCore Runtime service: POST /invocations streams a run as SSE.

`BedrockAgentCoreApp` provides the HTTP server, /ping and the SSE framing for an async generator.
Each run gets its own session, and with it its own microVM, so nothing is shared between incidents.
"""

import logging
from collections.abc import AsyncGenerator
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from pydantic import ValidationError

from incident_agent.config import Settings
from incident_agent.llm.factory import build_models
from incident_agent.runner import RunLimits, run_incident
from incident_agent.schemas import Incident, RunFailed
from incident_agent.telemetry import configure_telemetry

logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()


@app.entrypoint
async def invoke(payload: dict[str, Any]) -> AsyncGenerator[dict[str, Any], None]:
    try:
        incident = Incident.model_validate(payload.get("incident"))
    except ValidationError as error:
        logger.warning("Rejected an invalid incident: %s", error)
        yield RunFailed(error="The incident in this request is not valid.").model_dump(mode="json")
        return

    try:
        settings = Settings()
        limits = RunLimits(
            turns=settings.max_total_turns,
            total_tokens=settings.max_total_tokens,
            timeout_s=settings.run_timeout_s,
        )
        models = build_models(settings)
    except Exception:
        # Bad configuration is the operator's problem; the details belong in the logs, not the UI.
        logger.exception("The agent is misconfigured")
        yield RunFailed(error="The agent is misconfigured. See its logs.").model_dump(mode="json")
        return
    async for event in run_incident(incident, models, limits=limits):
        yield event.model_dump(mode="json")


if __name__ == "__main__":
    configure_telemetry(Settings())
    app.run()
