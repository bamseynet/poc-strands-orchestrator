"""ASGI entry point: `uvicorn incident_api.main:app`."""

from incident_agent.config import Settings
from incident_agent.telemetry import configure_telemetry
from incident_api.app import create_app
from incident_api.backends import build_backend
from incident_api.settings import ApiSettings, resolve_api_settings

_settings = Settings()
_api_settings = resolve_api_settings(ApiSettings())
configure_telemetry(_settings)
app = create_app(
    agent_settings=_settings,
    api_settings=_api_settings,
    backend=build_backend(_settings, _api_settings),
)
