"""Tests for label hygiene (sanitize_labels) — security fix 01-H1 (denylist only).

Covers the sensitive-key denylist, non-mutation, that legitimate labels pass
through untouched, and the integration with the Counter/Histogram/Gauge wrappers.
"""

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from neoflo_metrics._labels import sanitize_labels
from neoflo_metrics._types import Counter, Gauge


class TestSanitizeLabels:
    def test_none_and_empty(self):
        assert sanitize_labels(None) == {}
        assert sanitize_labels({}) == {}

    def test_legitimate_labels_pass_through(self):
        # No allowlist — any non-sensitive key is kept, including ones the SDK
        # never saw before (e.g. "client", a future label). This is the point.
        labels = {
            "tenant_id": "t1",
            "operation": "insert_one",
            "status": "success",
            "client": "zalora",
            "some_new_future_label": "v",
        }
        assert sanitize_labels(labels) == labels

    def test_denylisted_keys_dropped(self):
        for bad in ("password", "access_token", "api_key", "email", "user_id",
                    "session_id", "authorization", "invoice_id", "auth_header"):
            result = sanitize_labels({bad: "x", "status": "ok"})
            assert bad not in result, f"{bad} should be dropped"
            assert result["status"] == "ok"

    def test_case_insensitive_denylist(self):
        assert "Authorization" not in sanitize_labels({"Authorization": "Bearer x"})
        assert "API_KEY" not in sanitize_labels({"API_KEY": "x"})

    def test_does_not_mutate_input(self):
        original = {"status": "ok", "email": "a@b.com"}
        sanitize_labels(original)
        assert original == {"status": "ok", "email": "a@b.com"}

    def test_tenant_id_preserved(self):
        # tenant_id is a legitimate bounded dimension and is NOT on the denylist.
        assert sanitize_labels({"tenant_id": "acme"}) == {"tenant_id": "acme"}

    def test_client_resource_style_label_preserved(self):
        # "client" (from OTEL_RESOURCE_ATTRIBUTES) must never be dropped.
        assert sanitize_labels({"client": "zalora"}) == {"client": "zalora"}


def _reader_points(reader):
    md = reader.get_metrics_data()
    return md.resource_metrics[0].scope_metrics[0].metrics[0].data.data_points


class TestWrapperIntegration:
    def _counter(self):
        reader = InMemoryMetricReader()
        meter = MeterProvider(metric_readers=[reader]).get_meter("test")
        return Counter(meter.create_counter("c")), reader

    def test_counter_drops_sensitive_keeps_rest(self):
        counter, reader = self._counter()
        counter.add(1, {"status": "ok", "email": "a@b.com", "user_id": "u1", "client": "zalora"})
        attrs = _reader_points(reader)[0].attributes
        assert attrs.get("status") == "ok"
        assert attrs.get("client") == "zalora"   # legit label kept
        assert "email" not in attrs
        assert "user_id" not in attrs

    def test_gauge_drops_sensitive_label(self):
        reader = InMemoryMetricReader()
        meter = MeterProvider(metric_readers=[reader]).get_meter("test")
        gauge = Gauge(meter.create_up_down_counter("g"))
        gauge.set(10, {"queue": "a", "email": "x@y.com"})
        for p in _reader_points(reader):
            assert "email" not in p.attributes
            assert p.attributes.get("queue") == "a"
