"""ASGI entry point: `uvicorn incident_api.main:app`."""

from incident_agent.config import Settings
from incident_agent.telemetry import configure_telemetry
from incident_api.app import create_app

_settings = Settings()
configure_telemetry(_settings)
app = create_app(agent_settings=_settings)
