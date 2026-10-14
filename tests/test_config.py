"""Tests for MetricsConfig validation."""

import pytest
from neoflo_metrics._config import MetricsConfig


def test_valid_config_passes():
    cfg = MetricsConfig(service_name="svc", otlp_endpoint="http://localhost:4317")
    assert cfg.service_name == "svc"


def test_grpc_endpoint_passes():
    cfg = MetricsConfig(service_name="svc", otlp_endpoint="grpc://collector:4317")
    assert cfg.otlp_endpoint.startswith("grpc://")


def test_https_endpoint_passes():
    # Services deployed outside our own VPC reach the collector over the public
    # internet, so the endpoint is https. Rejecting it left those services with
    # no MeterProvider and no metrics at all.
    cfg = MetricsConfig(service_name="svc", otlp_endpoint="https://api.example.com/otel")
    assert cfg.otlp_endpoint.startswith("https://")


def test_empty_service_name_raises():
    with pytest.raises(ValueError, match="service_name cannot be empty"):
        MetricsConfig(service_name="", otlp_endpoint="http://localhost:4317")


def test_whitespace_service_name_raises():
    with pytest.raises(ValueError, match="service_name cannot be empty"):
        MetricsConfig(service_name="   ", otlp_endpoint="http://localhost:4317")


def test_bad_endpoint_raises():
    with pytest.raises(ValueError, match="otlp_endpoint must start with"):
        MetricsConfig(service_name="svc", otlp_endpoint="localhost:4317")


def test_short_interval_raises():
    with pytest.raises(ValueError, match="export_interval_ms must be >= 1000ms"):
        MetricsConfig(service_name="svc", otlp_endpoint="http://localhost:4317", export_interval_ms=500)


def test_minimum_interval_passes():
    cfg = MetricsConfig(service_name="svc", otlp_endpoint="http://localhost:4317", export_interval_ms=1000)
    assert cfg.export_interval_ms == 1000
