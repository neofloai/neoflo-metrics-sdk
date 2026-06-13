"""
Infrastructure metrics — HTTP request instrumentation and system resource tracking.

This module has two responsibilities:

1. HTTP instruments (used by MetricsMiddleware):
   Returns a dict of pre-created OTEL instruments that the middleware reads.
   Instruments are created lazily on first call and cached — avoids duplicate
   registration if middleware is instantiated more than once.

2. System metrics collection (SystemMetricsCollector):
   A background daemon thread that samples CPU, memory, and uptime every 30 s
   and registers ObservableGauge callbacks for CPU and RSS memory.

WHY daemon=True for the background thread:
    A non-daemon thread would keep the process alive after the main thread exits,
    preventing clean shutdown of uvicorn/gunicorn. Daemon threads are killed
    automatically when the interpreter exits, which is the correct behaviour
    for a background sampler with no cleanup requirements.

WHY 30-second sampling interval:
    System metrics (CPU %, RSS) are used for alerting on sustained resource
    pressure, not for millisecond-level profiling. Sampling every 30 s is
    sufficient to detect trends and reduces overhead vs. 1-5 s intervals.
    The OTel collector will interpolate between data points in dashboards.

WHY ObservableGauge for CPU and memory instead of UpDownCounter:
    UpDownCounter is a cumulative instrument — its data-model value is the
    running sum of all .add() deltas since process start. Emitting deltas
    (cpu_now - cpu_prev) makes that cumulative sum meaningless (it converges
    to the running average, not the current reading). ObservableGauge is the
    correct OTEL semantic for "current point-in-time value" — it reports the
    snapshot at each collection interval, exactly what Prometheus expects for
    gauge metrics. We store the latest snapshot in instance variables and read
    them from the callback registered with the OTEL SDK.

WHY a lock is required for the double-checked locking on _http_instruments:
    On CPython the GIL makes bare dict reads atomic, but free-threaded Python
    (3.13+ --disable-gil) removes that guarantee. We use a threading.Lock with
    explicit double-check to be correct under both runtimes.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import psutil

from ._config import get_config
from ._provider import get_meter

logger = logging.getLogger(__name__)

# Histogram bucket boundaries tuned for HTTP latency in milliseconds.
# Chosen to give good resolution at both fast (5–100 ms) and slow
# (250 ms–5 s) ends of the distribution, matching typical SLO thresholds.
# These are wired into the http_request_duration_ms histogram via a View.
DURATION_BOUNDARIES_MS: list[float] = [5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000]

# Cache for HTTP instruments — created once, reused by every request.
# Protected by _http_instruments_lock; double-checked for performance.
_http_instruments: dict[str, Any] | None = None
_http_instruments_lock = threading.Lock()


def get_http_instruments() -> dict[str, Any]:
    """Return (and lazily create) the shared HTTP metric instruments.

    WHY lazy initialization:
        The middleware may be instantiated before configure_metrics() in some
        test setups. Lazy init means we only call get_meter() when the first
        real request arrives, by which time the provider is guaranteed to exist.

    WHY double-checked locking:
        The outer check avoids lock acquisition on every request once initialized.
        The inner check inside the lock prevents double-initialization when two
        threads race on the first request simultaneously.
    """
    global _http_instruments

    # Outer check — no lock needed if already initialized (fast path).
    if _http_instruments is not None:
        return _http_instruments

    with _http_instruments_lock:
        # Inner check — re-check under the lock to handle concurrent first calls.
        if _http_instruments is not None:
            return _http_instruments

        meter = get_meter("neoflo.infra.http")
        cfg = get_config()
        common = {"service": cfg.service_name, "environment": cfg.environment}

        _http_instruments = {
            "duration": meter.create_histogram(
                name="http_request_duration_ms",
                description="End-to-end HTTP request latency in milliseconds",
                unit="ms",
                # Explicit boundaries are applied via a View registered on the
                # MeterProvider at initialization time (see _provider.py).
                # DURATION_BOUNDARIES_MS defines the intended bucket edges.
            ),
            "requests_total": meter.create_counter(
                name="http_requests_total",
                description="Total number of HTTP requests handled",
                unit="1",
            ),
            "in_flight": meter.create_up_down_counter(
                name="http_requests_in_flight",
                description="Number of HTTP requests currently being processed",
                unit="1",
            ),
            "errors_total": meter.create_counter(
                name="http_request_errors_total",
                description="Total number of HTTP 4xx/5xx responses",
                unit="1",
            ),
            # Store common labels so middleware can merge them with per-request labels.
            "_common_labels": common,
        }

    return _http_instruments


class SystemMetricsCollector:
    """Collects and exports process-level system metrics in the background.

    Metrics exported:
        process_cpu_usage_percent  — ObservableGauge, CPU % of this process
        process_memory_bytes       — ObservableGauge, RSS in bytes
        process_uptime_seconds     — monotonic Counter, seconds since process start

    WHY ObservableGauge for CPU and memory:
        These are point-in-time readings, not cumulative sums. ObservableGauge
        reports the current snapshot on each collection interval, which maps
        correctly to Prometheus gauge semantics after the OTEL → Prometheus
        pipeline. UpDownCounter would accumulate deltas into a meaningless sum.

    WHY a Counter for uptime:
        Uptime is genuinely cumulative — it never decreases — so Counter is the
        correct instrument. The OTEL collector converts it to a monotonic counter
        in Prometheus, enabling rate() and increase() queries.
    """

    def __init__(self) -> None:
        cfg = get_config()
        self._common_labels = {
            "service": cfg.service_name,
            "environment": cfg.environment,
        }

        meter = get_meter("neoflo.infra.system")

        # Latest snapshots, written by the background thread and read by the
        # ObservableGauge callbacks. Protected by _snapshot_lock.
        self._snapshot_lock = threading.Lock()
        self._latest_cpu: float = 0.0
        self._latest_memory: int = 0

        # ObservableGauge: OTEL calls our callback at each export interval and
        # we return the current reading. This is the correct semantic for a
        # metric whose value can both increase and decrease.
        meter.create_observable_gauge(
            name="process_cpu_usage_percent",
            description="CPU usage percentage of this process",
            unit="1",
            callbacks=[self._observe_cpu],
        )
        meter.create_observable_gauge(
            name="process_memory_bytes",
            description="RSS memory usage of this process in bytes",
            unit="By",
            callbacks=[self._observe_memory],
        )

        # Uptime is cumulative and monotone — Counter is correct here.
        self._uptime = meter.create_counter(
            name="process_uptime_seconds",
            description="Seconds elapsed since the process started",
            unit="s",
        )

        self._process = psutil.Process()
        self._start_time = self._process.create_time()
        self._last_uptime: float = 0.0

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # --- ObservableGauge callbacks ---
    # Called by the OTEL SDK at each export interval (on the exporter thread).
    # Must return an iterable of Observation objects.

    def _observe_cpu(self, options: Any) -> list:
        from opentelemetry.metrics import Observation
        with self._snapshot_lock:
            value = self._latest_cpu
        return [Observation(value, attributes=self._common_labels)]

    def _observe_memory(self, options: Any) -> list:
        from opentelemetry.metrics import Observation
        with self._snapshot_lock:
            value = self._latest_memory
        return [Observation(value, attributes=self._common_labels)]

    def start(self) -> None:
        """Spawn the background collection daemon thread."""
        self._thread = threading.Thread(
            target=self._collect_loop,
            name="neoflo-system-metrics",
            # daemon=True: killed automatically on interpreter exit so we never
            # block process shutdown waiting for the next 30-second sample.
            daemon=True,
        )
        self._thread.start()
        logger.debug("SystemMetricsCollector started (interval=30s)")

    def stop(self) -> None:
        """Signal the collection thread to exit. Used in tests."""
        self._stop_event.set()

    def _collect_loop(self) -> None:
        """Main loop: collect immediately, then every 30 s."""
        self._collect_once()
        while not self._stop_event.wait(timeout=30):
            self._collect_once()

    def _collect_once(self) -> None:
        """Sample system metrics and update the snapshot for ObservableGauge callbacks."""
        try:
            # cpu_percent(interval=None) returns usage since last call — low
            # overhead because we're not blocking the thread here.
            cpu = self._process.cpu_percent(interval=None)
            mem = self._process.memory_info().rss
            uptime = time.time() - self._start_time

            # Update snapshots under lock so ObservableGauge callbacks always
            # see a consistent pair of values.
            with self._snapshot_lock:
                self._latest_cpu = cpu
                self._latest_memory = mem

            # Uptime is a monotonic counter — emit the delta since last sample.
            uptime_delta = uptime - self._last_uptime
            self._uptime.add(uptime_delta, attributes=self._common_labels)
            self._last_uptime = uptime

        except (psutil.AccessDenied, psutil.NoSuchProcess) as exc:
            # Expected failure modes: process permissions changed or process
            # disappeared (unlikely for self-monitoring but possible in containers).
            logger.debug("SystemMetricsCollector: psutil access error: %s", exc)
        except Exception:
            # Unexpected error — log with full traceback but never propagate.
            # The service must keep running even if metrics collection fails.
            logger.exception("SystemMetricsCollector: unexpected error collecting metrics")
