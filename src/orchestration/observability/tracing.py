"""OpenTelemetry tracing setup for the orchestration service.

Initialises a ``TracerProvider`` with ``service.name=orchestration`` /
``service.version=0.1.0`` resource attributes and a ``BatchSpanProcessor``
that exports to ``ConsoleSpanExporter`` by default, switching to
``OTLPSpanExporter`` (gRPC) when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set.

The module-level ``tracer`` proxy is created via ``trace.get_tracer`` so
LLM clients and stage wrappers can import it and call
``with tracer.start_as_current_span("...") as span:`` without thinking
about whether ``init_tracing()`` has been called — the proxy resolves the
provider on each span start, so an early import before ``init_tracing()``
still records spans under whichever provider is active at span time.

``init_tracing()`` is idempotent: a second call after the global provider
is already configured is a no-op, which keeps the API server, worker,
and pytest fixtures all safe to call it without re-arming exporters.
"""
from __future__ import annotations

import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

# Module-level tracer proxy. Resolved by the SDK against whatever
# TracerProvider is current at the moment a span is started, so importing
# this before init_tracing() is safe.
tracer = trace.get_tracer("orchestration")

_SERVICE_NAME = "orchestration"
_SERVICE_VERSION = "0.1.0"


def init_tracing() -> None:
    """Set up the global ``TracerProvider`` with our resource attributes.

    Exports go to ``ConsoleSpanExporter`` in dev / tests; set
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` (e.g. ``http://localhost:4317``) to
    ship to an OTLP collector instead. ``OTEL_EXPORTER_OTLP_PROTOCOL``
    selects ``grpc`` (default) or ``http/protobuf``.

    Idempotent: if the global provider is already an instance of our
    ``TracerProvider``, this call is a no-op so repeated startup paths
    don't double-register processors.
    """
    existing = trace.get_tracer_provider()
    if isinstance(existing, TracerProvider):
        # Already initialised by us (or by something that happens to use
        # the SDK class). Don't pile on a second processor.
        print("tracing already initialized")
        return

    resource = Resource.create(
        {
            "service.name": _SERVICE_NAME,
            "service.version": _SERVICE_VERSION,
        }
    )
    provider = TracerProvider(resource=resource)

    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if otlp_endpoint:
        # Production / staging: ship to an OTLP collector. Honour the
        # OTEL_EXPORTER_OTLP_PROTOCOL env (defaults to grpc), matching
        # the spec's reference implementation behaviour.
        protocol = os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc").lower()
        if protocol == "http/protobuf":
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter as HttpOTLPSpanExporter,
            )

            exporter = HttpOTLPSpanExporter(endpoint=otlp_endpoint)
        else:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter as GrpcOTLPSpanExporter,
            )

            exporter = GrpcOTLPSpanExporter(endpoint=otlp_endpoint)
    else:
        exporter = ConsoleSpanExporter()

    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    print(f"tracing initialized (exporter={'otlp' if otlp_endpoint else 'console'})")