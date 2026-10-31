"""OpenTelemetry tracing, set up the way the Strands docs describe.

Strands creates spans whatever happens. This only decides where they go. Exporters are configured
through the standard OTEL_* environment variables.
"""

import os

from opentelemetry import trace as trace_api
from strands.telemetry import StrandsTelemetry

from incident_agent.config import Settings


def configure_telemetry(settings: Settings) -> bool:
    """Attach exporters if asked to, and return whether this call set anything up."""
    if not isinstance(trace_api.get_tracer_provider(), trace_api.ProxyTracerProvider):
        # Something already owns tracing: on AgentCore that is `opentelemetry-instrument` (ADOT).
        return False

    otlp = bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))
    if not otlp and not settings.otel_console_export:
        return False

    strands_telemetry = StrandsTelemetry()
    if otlp:
        strands_telemetry.setup_otlp_exporter()
    if settings.otel_console_export:
        strands_telemetry.setup_console_exporter()
    return True
