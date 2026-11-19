"""Tests for per-tenant labelling (X-Tenant-ID header, SQS, contextvar)."""

import asyncio

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from neoflo_metrics import _tenant
from neoflo_metrics._tenant import (
    MISSING_TENANT,
    OVERFLOW_TENANT,
    get_tenant,
    normalize_tenant,
    tenant_context,
)
from neoflo_metrics._provider import get_meter
from neoflo_metrics._types import Counter, Gauge, Histogram
from neoflo_metrics.helpers.sqs import sqs_handler_timer


@pytest.fixture(autouse=True)
def _clean_tenants(monkeypatch):
    monkeypatch.delenv("METRICS_MAX_TENANTS", raising=False)
    _tenant._reset_seen_for_tests()
    yield
    _tenant._reset_seen_for_tests()


def _points(reader, name):
    """Return {tenant_id: value} for a counter, from an InMemoryMetricReader."""
    out = {}
    data = reader.get_metrics_data()
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name != name:
                    continue
                for dp in metric.data.data_points:
                    out[dict(dp.attributes).get("tenant_id")] = dp.value
    return out


def _meter():
    reader = InMemoryMetricReader()
    return reader, MeterProvider(metric_readers=[reader]).get_meter("test")


class TestNormalize:
    def test_valid_value_passes_through(self):
        assert normalize_tenant("acme-1_x") == "acme-1_x"

    @pytest.mark.parametrize("raw", [None, "", "a b", "a/b", "x" * 65, "t;drop", 5])
    def test_missing_or_invalid_becomes_dash(self, raw):
        assert normalize_tenant(raw) == MISSING_TENANT

    def test_cap_overflows_to_other(self, monkeypatch):
        monkeypatch.setenv("METRICS_MAX_TENANTS", "3")
        assert [normalize_tenant(f"t{i}") for i in range(3)] == ["t0", "t1", "t2"]
        assert normalize_tenant("t3") == OVERFLOW_TENANT
        assert normalize_tenant("t0") == "t0"  # already-seen tenants keep working

    def test_default_cap_is_ten(self):
        results = [normalize_tenant(f"t{i}") for i in range(12)]
        assert results[:10] == [f"t{i}" for i in range(10)]
        assert results[10:] == [OVERFLOW_TENANT, OVERFLOW_TENANT]

    def test_bad_cap_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("METRICS_MAX_TENANTS", "lots")
        assert normalize_tenant("a") == "a"


class TestContextLabels:
    def test_counter_gets_context_tenant(self):
        reader, meter = _meter()
        counter = Counter(meter.create_counter("c"))
        with tenant_context("acme"):
            counter.add(1)
        counter.add(1)  # no tenant -> no label
        assert _points(reader, "c") == {"acme": 1, None: 1}

    def test_explicit_label_wins_over_context(self):
        reader, meter = _meter()
        counter = Counter(meter.create_counter("c"))
        with tenant_context("acme"):
            counter.add(1, {"tenant_id": "explicit"})
        assert _points(reader, "c") == {"explicit": 1}

    def test_histogram_and_gauge_get_tenant(self):
        reader, meter = _meter()
        hist = Histogram(meter.create_histogram("h"))
        gauge = Gauge(meter.create_up_down_counter("g"))
        with tenant_context("a"):
            hist.record(5)
            gauge.set(3)
        with tenant_context("b"):
            gauge.set(7)
        assert _points(reader, "g") == {"a": 3, "b": 7}

    @pytest.mark.asyncio
    async def test_concurrent_tasks_do_not_leak_tenant(self):
        seen = {}

        async def work(name):
            with tenant_context(name):
                await asyncio.sleep(0)
                seen[name] = get_tenant()

        await asyncio.gather(work("a"), work("b"), work("c"))
        assert seen == {"a": "a", "b": "b", "c": "c"}
        assert get_tenant() is None


class TestMiddleware:
    def _client(self):
        from starlette.applications import Starlette
        from starlette.responses import Response
        from starlette.routing import Route
        from starlette.testclient import TestClient

        from neoflo_metrics.middleware import MetricsMiddleware

        def ok(request):
            return Response(status_code=200)

        app = Starlette(routes=[Route("/ok", ok)])
        app.add_middleware(MetricsMiddleware)
        return TestClient(app)

    def test_two_tenants_give_two_series(self, inmemory_sdk):
        client = self._client()
        client.get("/ok", headers={"X-Tenant-ID": "acme"})
        client.get("/ok", headers={"X-Tenant-ID": "acme"})
        client.get("/ok", headers={"X-Tenant-ID": "globex"})
        assert _points(inmemory_sdk, "http_requests_total") == {"acme": 2, "globex": 1}

    def test_missing_and_invalid_header_become_dash(self, inmemory_sdk):
        client = self._client()
        client.get("/ok")
        client.get("/ok", headers={"X-Tenant-ID": "bad value!"})
        assert _points(inmemory_sdk, "http_requests_total") == {MISSING_TENANT: 2}

    def test_excess_tenants_become_other(self, inmemory_sdk, monkeypatch):
        monkeypatch.setenv("METRICS_MAX_TENANTS", "2")
        client = self._client()
        for tenant in ["t0", "t1", "t2", "t3"]:
            client.get("/ok", headers={"X-Tenant-ID": tenant})
        assert _points(inmemory_sdk, "http_requests_total") == {
            "t0": 1, "t1": 1, OVERFLOW_TENANT: 2,
        }

    def test_business_metric_inside_request_inherits_tenant(self, inmemory_sdk):
        from starlette.applications import Starlette
        from starlette.responses import Response
        from starlette.routing import Route
        from starlette.testclient import TestClient

        from neoflo_metrics.middleware import MetricsMiddleware

        biz = Counter(get_meter("biz").create_counter("erp_sync_total"))

        async def sync(request):
            biz.add(1, {"erp_type": "sap"})
            return Response(status_code=200)

        app = Starlette(routes=[Route("/sync", sync)])
        app.add_middleware(MetricsMiddleware)
        client = TestClient(app)
        client.get("/sync", headers={"X-Tenant-ID": "acme"})
        client.get("/sync", headers={"X-Tenant-ID": "globex"})
        assert _points(inmemory_sdk, "erp_sync_total") == {"acme": 1, "globex": 1}

    def test_tenant_is_reset_after_request(self, inmemory_sdk):
        self._client().get("/ok", headers={"X-Tenant-ID": "acme"})
        assert get_tenant() is None


class _FakeMetrics:
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


class TestSqsHelper:
    @pytest.mark.asyncio
    async def test_success_labels_tenant(self):
        reader, meter = _meter()
        metrics = _FakeMetrics(meter)
        async with sqs_handler_timer(metrics, queue_name="q", tenant_id="acme"):
            pass
        async with sqs_handler_timer(metrics, queue_name="q", tenant_id="globex"):
            pass
        assert _points(reader, "sqs_messages_processed_total") == {"acme": 1, "globex": 1}
        assert get_tenant() is None

    @pytest.mark.asyncio
    async def test_error_labels_tenant_and_reraises(self):
        reader, meter = _meter()
        metrics = _FakeMetrics(meter)
        with pytest.raises(ValueError):
            async with sqs_handler_timer(metrics, queue_name="q", tenant_id="acme"):
                raise ValueError("boom")
        assert _points(reader, "sqs_message_errors_total") == {"acme": 1}
        assert get_tenant() is None

    @pytest.mark.asyncio
    async def test_no_tenant_leaves_labels_unchanged(self):
        reader, meter = _meter()
        metrics = _FakeMetrics(meter)
        async with sqs_handler_timer(metrics, queue_name="q"):
            pass
        assert _points(reader, "sqs_messages_processed_total") == {None: 1}
