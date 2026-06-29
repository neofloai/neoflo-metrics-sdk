"""
neoflo-metrics: Neoflo platform metrics SDK for Python microservices.

Public API surface — the only imports services should need:

    from neoflo_metrics import configure_metrics, create_metrics
    from neoflo_metrics.middleware import MetricsMiddleware

Everything prefixed with _ is internal implementation detail and may change
between minor versions without notice.

Initialization order (enforced by configure_metrics):
    1. MetricsConfig created and stored as global singleton
    2. OTel MeterProvider created and globally registered
    3. SystemMetricsCollector daemon thread started

WHY eager SystemMetricsCollector startup here:
    Starting the collector inside configure_metrics() (rather than on first
    request) ensures process_uptime_seconds begins accumulating from process
    start, not from when the first HTTP request arrives. This matters for
    batch workers or services with slow startup that might take minutes to
    serve their first request.

WHY a single _initialized flag guards all three setup steps:
    initialize_provider() is idempotent on its own, but set_config() would
    silently overwrite the config and SystemMetricsCollector().start() would
    spawn a second daemon thread on a second call. Rather than adding guards
    in each helper, we front-load the check here so configure_metrics() is
    fully idempotent as a unit. This matches the semantics of logging.basicConfig().
"""

from __future__ import annotations

import threading

from ._business import BusinessMetrics, MetricSpec, SLASpec, create_metrics
from ._config import MetricsConfig, get_config, set_config
from ._infra import SystemMetricsCollector
from ._provider import (
    initialize_provider,
    SQS_PROCESSING_BOUNDARIES_MS,
    MONGO_DURATION_BOUNDARIES_MS,
    CLAUDE_COST_BOUNDARIES_USD,
    HTTP_SLA_MS,
    SQS_SLA_MS,
    MONGO_SLA_MS,
    CLAUDE_DURATION_SLA_MS,
    CLAUDE_COST_SLA_USD,
)

__all__ = [
    "configure_metrics",
    "shutdown_metrics",
    "create_metrics",
    "MetricSpec",
    "SLASpec",
    "MetricsConfig",
    "BusinessMetrics",
    "SQS_PROCESSING_BOUNDARIES_MS",
    "MONGO_DURATION_BOUNDARIES_MS",
    "CLAUDE_COST_BOUNDARIES_USD",
    "HTTP_SLA_MS",
    "SQS_SLA_MS",
    "MONGO_SLA_MS",
    "CLAUDE_DURATION_SLA_MS",
    "CLAUDE_COST_SLA_USD",
]

# Guards against double-initialization (e.g., configure_metrics() called twice
# in tests or by misconfigured service startup code).
_initialized = False
_init_lock = threading.Lock()
_collector: SystemMetricsCollector | None = None


def configure_metrics(
    service_name: str,
    otlp_endpoint: str,
    environment: str = "production",
    export_interval_ms: int = 5000,
) -> None:
    """Initialize the metrics SDK. Call exactly once at process startup.

    Subsequent calls are no-ops — the first call wins. This mirrors the
    behaviour of Python's logging.basicConfig() and avoids leaking exporter
    connections or background threads on duplicate calls.

    Args:
        service_name:      Identifier for this service (e.g., "invoice-validator-be").
                           Attached as a label to all infrastructure metrics.
        otlp_endpoint:     gRPC endpoint of the OTel collector
                           (e.g., "http://otel-collector:4317").
        environment:       Deployment environment label ("production", "staging", etc.).
        export_interval_ms: How often to push metrics to the collector, in ms.
                           Lower values increase freshness but add collector load.
    """
    global _initialized

    # Fast path — already initialized, no work needed.
    if _initialized:
        return

    with _init_lock:
        # Re-check inside the lock to handle concurrent startup (e.g., multiple
        # uvicorn worker processes calling configure_metrics at the same time).
        if _initialized:
            return

        cfg = MetricsConfig(
            service_name=service_name,
            otlp_endpoint=otlp_endpoint,
            environment=environment,
            export_interval_ms=export_interval_ms,
        )

        set_config(cfg)

        # Initialize OTel MeterProvider and register it globally so that
        # opentelemetry-instrumentation-* libraries use the same backend.
        initialize_provider(cfg)

        # Start system metrics collection immediately — not on first request —
        # so uptime and resource metrics are available from process boot.
        global _collector
        _collector = SystemMetricsCollector()
        _collector.start()

        _initialized = True


def shutdown_metrics() -> None:
    """Stop background metrics collection. Call from ASGI lifespan shutdown.

    Safe to call even if configure_metrics() was never called (no-op).
    Allows clean teardown in tests and on ECS container SIGTERM so the last
    system metric readings are flushed before the process exits.
    """
    if _collector is not None:
        _collector.stop()
