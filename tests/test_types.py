"""Tests for Counter, Histogram, Gauge wrapper types."""

import threading
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from neoflo_metrics._types import Gauge


def _make_gauge() -> tuple[Gauge, InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter("test")
    instrument = meter.create_up_down_counter("test_gauge")
    return Gauge(instrument), reader


class TestGaugeMultiLabel:
    def test_single_label_set(self):
        gauge, reader = _make_gauge()
        gauge.set(10, {"queue": "a"})
        gauge.set(15, {"queue": "a"})
        # Both calls should produce correct deltas: +10 then +5
        metrics = reader.get_metrics_data()
        points = metrics.resource_metrics[0].scope_metrics[0].metrics[0].data.data_points
        # The UpDownCounter accumulates: final value should be 15
        assert any(p.value == 15 for p in points)

    def test_multi_label_isolation(self):
        """set() on one label combo must not affect another label's delta."""
        gauge, reader = _make_gauge()
        gauge.set(10, {"queue": "init_queue"})
        gauge.set(5, {"queue": "slow_tools_queue"})
        # slow_tools_queue should have value 5, NOT -5 (the old bug)
        metrics = reader.get_metrics_data()
        points = metrics.resource_metrics[0].scope_metrics[0].metrics[0].data.data_points
        by_label = {tuple(sorted(p.attributes.items())): p.value for p in points}
        assert by_label.get((("queue", "init_queue"),)) == 10
        assert by_label.get((("queue", "slow_tools_queue"),)) == 5

    def test_concurrent_set_no_corruption(self):
        """Two threads calling .set() concurrently must not corrupt state."""
        gauge, _ = _make_gauge()
        errors = []

        def worker(val: int, label: str):
            try:
                for _ in range(100):
                    gauge.set(val, {"worker": label})
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=worker, args=(10, "a"))
        t2 = threading.Thread(target=worker, args=(20, "b"))
        t1.start(); t2.start()
        t1.join(); t2.join()
        assert not errors

    def test_add_tracks_per_label(self):
        gauge, reader = _make_gauge()
        gauge.add(5, {"queue": "a"})
        gauge.add(3, {"queue": "a"})
        metrics = reader.get_metrics_data()
        points = metrics.resource_metrics[0].scope_metrics[0].metrics[0].data.data_points
        assert any(p.value == 8 for p in points)
