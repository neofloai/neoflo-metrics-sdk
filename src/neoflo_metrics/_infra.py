"""
Infrastructure metrics — HTTP request instrumentation and system resource tracking.

This module has two responsibilities:

1. HTTP instruments (used by MetricsMiddleware):
   Returns a dict of pre-created OTEL instruments that the middleware reads.
   Instruments are created lazily on first call and cached — avoids duplicate
   registration if middleware is instantiated more than once.

2. System metrics collection (SystemMetricsCollector):
   A background daemon thread that samples CPU, memory, and uptime every 30 s
   and pushes values to OTEL gauges.

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
DURATION_BOUNDARIES_MS = [5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000]

# Cache for HTTP instruments — created once, reused by every request.
_http_instruments: dict[str, Any] | None = None
_http_instruments_lock = threading.Lock()


def get_http_instruments() -> dict[str, Any]:
    """Return (and lazily create) the shared HTTP metric instruments.

    WHY lazy initialization:
        The middleware may be instantiated before configure_metrics() in some
        test setups. Lazy init means we only call get_meter() when the first
        real request arrives, by which time the provider is guaranteed to exist.
    """
    global _http_instruments

    if _http_instruments is not None:
        return _http_instruments

    with _http_instruments_lock:
        # Double-checked locking: re-check inside the lock.
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
                # explicit_bucket_boundaries is set via a View in production;
                # we document the intended boundaries here for reference.
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
        process_cpu_usage_percent  — gauge, CPU % of this process
        process_memory_bytes       — gauge, RSS (resident set size) in bytes
        process_uptime_seconds     — counter, seconds since process start

    Instruments use UpDownCounter for CPU and memory (values can decrease
    between samples) and a monotonic Counter for uptime.
    """

    def __init__(self) -> None:
        cfg = get_config()
        self._common_labels = {
            "service": cfg.service_name,
            "environment": cfg.environment,
        }

        meter = get_meter("neoflo.infra.system")

        # UpDownCounter for mutable gauges (CPU %, RSS can decrease).
        self._cpu = meter.create_up_down_counter(
            name="process_cpu_usage_percent",
            description="CPU usage percentage of this process",
            unit="1",
        )
        self._memory = meter.create_up_down_counter(
            name="process_memory_bytes",
            description="RSS memory usage of this process in bytes",
            unit="By",
        )
        self._uptime = meter.create_counter(
            name="process_uptime_seconds",
            description="Seconds elapsed since the process started",
            unit="s",
        )

        self._process = psutil.Process()
        self._start_time = self._process.create_time()

        # Track previous values so UpDownCounters can emit deltas.
        self._last_cpu: float = 0.0
        self._last_memory: int = 0
        self._last_uptime: float = 0.0

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

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
        """Sample system metrics and push deltas to OTEL instruments."""
        try:
            # cpu_percent(interval=None) returns usage since last call — low
            # overhead because we're not blocking here.
            cpu = self._process.cpu_percent(interval=None)
            mem = self._process.memory_info().rss
            uptime = time.time() - self._start_time

            labels = self._common_labels

            # Emit delta for CPU (UpDownCounter needs deltas, not absolutes).
            self._cpu.add(cpu - self._last_cpu, attributes=labels)
            self._memory.add(mem - self._last_memory, attributes=labels)
            self._uptime.add(uptime - self._last_uptime, attributes=labels)

            self._last_cpu = cpu
            self._last_memory = mem
            self._last_uptime = uptime

        except Exception:
            # Never let a collection failure propagate — the service must keep
            # running even if psutil returns unexpected data.
            logger.exception("SystemMetricsCollector: failed to collect metrics")
