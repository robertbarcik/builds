"""OpenTelemetry tracing setup — exports spans to Dynatrace via OTLP/HTTP."""

import logging
import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

logger = logging.getLogger(__name__)

_DT_ENDPOINT = os.environ.get(
    "DT_ENDPOINT", "https://localhost:9411/api/v2/otlp/v1/traces"
)
_DT_API_TOKEN = os.environ.get("DT_API_TOKEN", "")


def init_tracing() -> trace.Tracer:
    """Initialize OTel tracer with Dynatrace OTLP exporter. Returns the app tracer."""
    resource = Resource.create(
        {
            "service.name": "mu",
            "service.version": "1.0.0",
            "deployment.environment": "demo",
        }
    )

    provider = TracerProvider(resource=resource)

    # Only export when Dynatrace is actually configured — otherwise the
    # batch processor retries against a dead endpoint on every flush.
    if _DT_API_TOKEN:
        exporter = OTLPSpanExporter(
            endpoint=_DT_ENDPOINT,
            headers={"Authorization": f"Api-Token {_DT_API_TOKEN}"},
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        logger.info("OpenTelemetry tracing initialized — exporting to Dynatrace")
    else:
        logger.info("OpenTelemetry tracing initialized — no DT_API_TOKEN, spans not exported")

    trace.set_tracer_provider(provider)
    return trace.get_tracer("mu.app", "1.0.0")


# Module-level tracer — import this from other modules
tracer = init_tracing()
