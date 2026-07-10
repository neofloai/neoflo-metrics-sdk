"""Shared fixtures for neoflo-metrics SDK tests."""

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

import neoflo_metrics._provider as _provider_module
import neoflo_metrics._config as _config_module
import neoflo_metrics._infra as _infra_module
import neoflo_metrics as sdk


@pytest.fixture
def inmemory_sdk():
    """Initialize the SDK with an InMemoryMetricReader instead of OTLP.

    Resets all module-level singletons after the test so tests don't bleed
    into each other.
    """
    reader = InMemoryMetricReader()

    from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
    http_view = View(
        instrument_name="http_request_duration_ms",
        aggregation=ExplicitBucketHistogramAggregation(
            boundaries=[5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000]
        ),
    )
    provider = MeterProvider(metric_readers=[reader], views=[http_view])

    from opentelemetry import metrics as otel_metrics
    otel_metrics.set_meter_provider(provider)

    _provider_module._meter_provider = provider

    from neoflo_metrics._config import MetricsConfig, set_config
    set_config(MetricsConfig(
        service_name="test-service",
        otlp_endpoint="http://localhost:4317",
        environment="test",
    ))

    sdk._initialized = True
    sdk._collector = None

    yield reader

    # Teardown — reset all singletons so the next test starts clean.
    sdk._initialized = False
    sdk._collector = None
    _provider_module._meter_provider = None
    _config_module._config = None
    # _http_instruments is a lazily-created module-level cache bound to
    # whichever MeterProvider was active on its first call (see _infra.py).
    # Without resetting it here, the next test's fresh provider/reader is
    # silently never used — instruments keep recording into this test's
    # already-torn-down provider.
    _infra_module._http_instruments = None
    otel_metrics.set_meter_provider(otel_metrics.NoOpMeterProvider())
