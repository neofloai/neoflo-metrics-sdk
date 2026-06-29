"""Tests for SystemMetricsCollector."""

import pytest
from unittest.mock import patch


def test_cpu_warmup_call_is_made():
    """SystemMetricsCollector.__init__ must call cpu_percent() once for warmup."""
    from neoflo_metrics._infra import SystemMetricsCollector
    import neoflo_metrics._config as cfg_module
    from neoflo_metrics._config import MetricsConfig, set_config
    import neoflo_metrics._provider as prov_module
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    # Set up minimal SDK state.
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    prov_module._meter_provider = provider
    set_config(MetricsConfig(service_name="test", otlp_endpoint="http://localhost:4317"))

    call_count = []

    import psutil

    original_cpu = psutil.Process.cpu_percent

    def counting_cpu(self, interval=None):
        call_count.append(1)
        return 0.0

    with patch.object(psutil.Process, "cpu_percent", counting_cpu):
        collector = SystemMetricsCollector()

    # Must have been called once during __init__ for the warm-up.
    assert len(call_count) >= 1

    # Cleanup.
    prov_module._meter_provider = None
    cfg_module._config = None
