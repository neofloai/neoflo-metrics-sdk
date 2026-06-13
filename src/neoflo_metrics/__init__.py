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
"""

from __future__ import annotations

from ._business import BusinessMetrics, create_metrics
from ._config import MetricsConfig, get_config, set_config
from ._infra import SystemMetricsCollector
from ._provider import initialize_provider

__all__ = [
    "configure_metrics",
    "create_metrics",
    "MetricsConfig",
    "BusinessMetrics",
]


def configure_metrics(
    service_name: str,
    otlp_endpoint: str,
    environment: str = "production",
    export_interval_ms: int = 5000,
) -> None:
    """Initialize the metrics SDK. Call exactly once at process startup.

    Args:
        service_name:      Identifier for this service (e.g., "invoice-validator-be").
                           Attached as a label to all infrastructure metrics.
        otlp_endpoint:     gRPC endpoint of the OTel collector
                           (e.g., "http://otel-collector:4317").
        environment:       Deployment environment label ("production", "staging", etc.).
        export_interval_ms: How often to push metrics to the collector, in ms.
                           Lower values increase freshness but add collector load.

    Raises:
        RuntimeError: If called after the provider is already initialized
                      (safe to ignore in test environments that reset state).
    """
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
    collector = SystemMetricsCollector()
    collector.start()
