import os

from orchestration.api.app import create_app
from orchestration.observability.tracing import init_tracing

# Initialise OpenTelemetry once on module import so LLM clients and
# stage wrappers can start emitting spans as soon as they're imported.
# Skipped under pytest (TESTING=1) to keep test output clean and avoid
# hanging the suite on OTLP exporters that may point at unreachable
# collector endpoints.
if not os.environ.get("TESTING"):
    init_tracing()

app = create_app()