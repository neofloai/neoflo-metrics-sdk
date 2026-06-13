"""
Stable wrapper types for OpenTelemetry metric instruments.

WHY we wrap OTEL instruments instead of exposing them directly:
    1. API stability: The OTEL Python SDK is still maturing. Wrapping insulates
       service code from breaking changes in the upstream SDK (e.g., the
       Observation/UpDownCounter rename history).
    2. Type safety: Services get Counter, Histogram, Gauge — not the OTEL
       internal types which have broader, less-typed interfaces.
    3. Label ergonomics: Services pass plain Python dicts; we convert to OTEL
       Attributes here in one place.
    4. Future extensibility: We can add validation, sampling, or rate-limiting
       inside the wrapper without touching 5+ service codebases.

Gauge implementation note:
    The OTEL Python SDK does not have an imperative Gauge with .set() semantics
    for synchronous code paths. The options are:
      a) ObservableGauge — callback-based, requires registering a function that
         is called at collection time. Awkward for imperative code where the
         current value is set by business logic at arbitrary times.
      b) UpDownCounter — imperative add(delta) semantics. We track the current
         value internally and compute the delta on each .set() call. This gives
         true gauge semantics (absolute value) over an additive instrument.

    We choose (b) because it matches the `metrics.invoices_pending.set(42)` API
    that services expect, while staying within the OTEL spec.
"""

from __future__ import annotations

import threading

from opentelemetry.sdk.metrics import MeterProvider  # noqa: F401 (type annotation)
from opentelemetry import metrics as otel_metrics


Labels = dict[str, str] | None


class Counter:
    """Monotonically increasing counter. Use for totals (requests, errors, etc.)."""

    def __init__(self, instrument: otel_metrics.Counter) -> None:
        self._instrument = instrument

    def add(self, value: int | float, labels: Labels = None) -> None:
        """Increment the counter by value. Labels become OTEL Attributes."""
        self._instrument.add(value, attributes=labels or {})


class Histogram:
    """Records distributions of values. Use for latencies, sizes, etc."""

    def __init__(self, instrument: otel_metrics.Histogram) -> None:
        self._instrument = instrument

    def record(self, value: int | float, labels: Labels = None) -> None:
        """Record a single observation. Labels become OTEL Attributes."""
        self._instrument.record(value, attributes=labels or {})


class Gauge:
    """Tracks an absolute current value that can go up or down.

    Implemented over UpDownCounter with internal state tracking so that
    .set(42) translates to add(42 - current_value) on the underlying counter.

    Thread-safety: _lock protects _current_value from concurrent .set()/.add()
    calls in multi-threaded ASGI servers (uvicorn workers, etc.).
    """

    def __init__(self, instrument: otel_metrics.UpDownCounter) -> None:
        self._instrument = instrument
        self._current_value: float = 0.0
        self._lock = threading.Lock()

    def set(self, value: int | float, labels: Labels = None) -> None:
        """Set the gauge to an absolute value."""
        with self._lock:
            delta = value - self._current_value
            self._current_value = float(value)
        # Record outside the lock: OTEL instruments are thread-safe internally.
        self._instrument.add(delta, attributes=labels or {})

    def add(self, value: int | float, labels: Labels = None) -> None:
        """Increment or decrement the gauge by a relative amount."""
        with self._lock:
            self._current_value += value
        self._instrument.add(value, attributes=labels or {})
