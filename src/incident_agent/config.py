"""Runtime configuration, read from the environment or a local `.env` file."""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_mode: Literal["fake", "bedrock"] = "fake"

    # Geo inference profiles: see the "Programmatic Access" section of each Bedrock model card.
    # Opus 5.5 and Haiku 5.5 have no in-region id in us-east-1, so they need a profile id there.
    bedrock_model_id: str = "us.anthropic.claude-opus-5-5"
    bedrock_worker_model_id: str = "us.anthropic.claude-haiku-5-5"
    aws_region: str = "us-east-1"

    run_timeout_s: float = Field(default=300.0, gt=0)
    # Shared by the orchestrator and all its workers: Strands counts these across the whole request.
    max_total_turns: int = Field(default=40, gt=0)
    max_total_tokens: int = Field(default=500_000, gt=0)

    # Paces the fake LLM so that a demo is watchable. 0 runs flat out, as the tests do.
    fake_llm_delay_scale: float = Field(default=1.0, ge=0)

    # Prints spans locally. For a collector, set the standard OTEL_EXPORTER_OTLP_ENDPOINT instead.
    otel_console_export: bool = False

    # The standard OTEL variables. pydantic-settings reads `.env` without exporting it, and the
    # OTel SDK reads only the real environment, so telemetry.py copies these across.
    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str | None = None
