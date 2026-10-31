"""Settings for the API itself. The agent's own settings live in `incident_agent.config`."""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # When set, POST /api/runs needs an "X-Access-Token" header. Leave unset only on localhost.
    api_access_token: str | None = None
    # Every run spends money on a public endpoint, so concurrent runs are capped.
    max_concurrent_runs: int = Field(default=5, gt=0)
    sse_heartbeat_s: float = Field(default=15.0, gt=0)
    # inprocess runs the agent inside the API. agentcore calls it on Bedrock AgentCore Runtime.
    agent_backend: Literal["inprocess", "agentcore"] = "inprocess"
