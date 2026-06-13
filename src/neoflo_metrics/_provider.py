"""
OpenTelemetry MeterProvider bootstrap for the neoflo-metrics SDK.

This module creates the single MeterProvider that all metric instruments are
registered under. It configures push-based export via OTLP gRPC to the
collector sidecar — no scrape endpoint is needed in the service itself.

WHY PeriodicExportingMetricReader (push) instead of pull/scrape:
    Prometheus pull requires the service to expose a /metrics HTTP endpoint,
    adding a dependency on the HTTP server and complicating network policy.
    Push via OTLP gRPC lets the OTel collector aggregate from many sources
    without per-service scrape config. The collector handles downsampling,
    relabelling, and fanout to Prometheus, Datadog, etc.

WHY set the global MeterProvider via set_meter_provider():
    The OTEL SDK resolves instruments via the globally-registered MeterProvider.
    If we kept our provider local, any code that calls
    opentelemetry.metrics.get_meter_provider() (e.g., third-party libraries
    like opentelemetry-instrumentation-*) would get a NoopMeterProvider and
    silently drop metrics. Setting the global ensures all OTEL-aware code in
    the process shares the same backend.
"""

from __future__ import annotations

from opentelemetry import metrics as otel_metrics
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

from ._config import MetricsConfig

# Module-level reference so we can check for double-initialization.
_meter_provider: MeterProvider | None = None


def initialize_provider(config: MetricsConfig) -> None:
    """Create and globally register the MeterProvider.

    Idempotent — subsequent calls are no-ops so that test fixtures that call
    configure_metrics() more than once don't stack exporters.
    """
    global _meter_provider

    if _meter_provider is not None:
        # Already initialized; skip to avoid stacking readers.
        return

    exporter = OTLPMetricExporter(
        endpoint=config.otlp_endpoint,
        # insecure=True is intentional for internal cluster traffic where
        # mTLS is handled at the service mesh layer (Istio/Linkerd), not
        # at the application layer.
        insecure=True,
    )

    reader = PeriodicExportingMetricReader(
        exporter=exporter,
        # export_interval_millis controls the push cadence. Default 5 s gives
        # sub-10-second metric freshness in dashboards without hammering the
        # collector with tiny batches.
        export_interval_millis=config.export_interval_ms,
    )

    _meter_provider = MeterProvider(metric_readers=[reader])

    # Register globally so opentelemetry-instrumentation-* libraries and any
    # future SDK helpers automatically use the same backend.
    otel_metrics.set_meter_provider(_meter_provider)


def get_meter(name: str) -> otel_metrics.Meter:
    """Return a Meter scoped to the given instrumentation library name.

    WHY not pass the MeterProvider around:
        Callers (infra, business) shouldn't need to know whether the provider
        has been initialized — get_meter() handles that and raises clearly.
    """
    if _meter_provider is None:
        raise RuntimeError(
            "MeterProvider not initialized. "
            "Ensure configure_metrics() has been called before get_meter()."
        )
    return _meter_provider.get_meter(name)
