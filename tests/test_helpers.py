"""Tests for SQS and MongoDB helper context managers."""

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from neoflo_metrics._types import Counter, Histogram
from neoflo_metrics.helpers.sqs import sqs_handler_timer
from neoflo_metrics.helpers.mongodb import mongo_op_timer


def _make_reader_and_meter():
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    return reader, provider.get_meter("test")


class FakeMetrics:
    """Minimal BusinessMetrics stand-in for helper tests."""

    def __init__(self, meter):
        self.sqs_message_processing_duration_ms = Histogram(
            meter.create_histogram("sqs_message_processing_duration_ms")
        )
        self.sqs_messages_processed_total = Counter(
            meter.create_counter("sqs_messages_processed_total")
        )
        self.sqs_message_errors_total = Counter(
            meter.create_counter("sqs_message_errors_total")
        )
        self.mongodb_operation_duration_ms = Histogram(
            meter.create_histogram("mongodb_operation_duration_ms")
        )
        self.mongodb_operations_total = Counter(
            meter.create_counter("mongodb_operations_total")
        )
        self.mongodb_errors_total = Counter(
            meter.create_counter("mongodb_errors_total")
        )


@pytest.mark.asyncio
async def test_sqs_timer_records_on_success():
    reader, meter = _make_reader_and_meter()
    metrics = FakeMetrics(meter)

    async with sqs_handler_timer(metrics, queue_name="init_queue"):
        pass  # simulates a successful handler

    data = reader.get_metrics_data()
    names = {
        m.name
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
    }
    assert "sqs_message_processing_duration_ms" in names
    assert "sqs_messages_processed_total" in names


@pytest.mark.asyncio
async def test_sqs_timer_records_error_on_exception():
    reader, meter = _make_reader_and_meter()
    metrics = FakeMetrics(meter)

    with pytest.raises(ValueError):
        async with sqs_handler_timer(metrics, queue_name="slow_tools_queue"):
            raise ValueError("handler failed")

    data = reader.get_metrics_data()
    names = {
        m.name
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
    }
    assert "sqs_message_errors_total" in names
    # duration must NOT be recorded on failure
    assert "sqs_message_processing_duration_ms" not in names


@pytest.mark.asyncio
async def test_mongo_timer_records_on_success():
    reader, meter = _make_reader_and_meter()
    metrics = FakeMetrics(meter)

    async with mongo_op_timer(metrics, operation="find", collection="invoices"):
        pass

    data = reader.get_metrics_data()
    names = {
        m.name
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
    }
    assert "mongodb_operation_duration_ms" in names
    assert "mongodb_operations_total" in names


@pytest.mark.asyncio
async def test_mongo_timer_records_error_on_exception():
    reader, meter = _make_reader_and_meter()
    metrics = FakeMetrics(meter)

    with pytest.raises(RuntimeError):
        async with mongo_op_timer(metrics, operation="insert", collection="runs"):
            raise RuntimeError("mongo unavailable")

    data = reader.get_metrics_data()
    names = {
        m.name
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
    }
    assert "mongodb_errors_total" in names
