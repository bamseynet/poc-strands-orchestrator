"""Settings for the API itself. The agent's own settings live in `incident_agent.config`."""

from typing import Any, Literal

import boto3
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # When set, POST /api/runs needs an "X-Access-Token" header. Leave unset only on localhost.
    api_access_token: str | None = None
    # A deployment references the token in Secrets Manager, so its environment never holds it.
    api_access_token_secret_arn: str | None = None
    # Every run spends money on a public endpoint, so concurrent runs are capped.
    max_concurrent_runs: int = Field(default=5, gt=0)
    sse_heartbeat_s: float = Field(default=15.0, gt=0)
    # inprocess runs the agent inside the API. agentcore calls it on Bedrock AgentCore Runtime.
    agent_backend: Literal["inprocess", "agentcore"] = "inprocess"
    agent_runtime_arn: str | None = None


def resolve_api_settings(settings: ApiSettings, secrets_client: Any = None) -> ApiSettings:
    """Fetch the access token if it is referenced. Fails rather than start unprotected."""
    if settings.api_access_token_secret_arn:
        client = secrets_client or boto3.client("secretsmanager")
        secret = client.get_secret_value(SecretId=settings.api_access_token_secret_arn)
        token = secret["SecretString"].strip()
        if not token:
            raise ValueError("The access token secret is empty")
        settings = settings.model_copy(update={"api_access_token": token})

    if settings.agent_backend == "agentcore" and not settings.api_access_token:
        raise ValueError("A deployment calling AgentCore must have an access token")
    return settings
