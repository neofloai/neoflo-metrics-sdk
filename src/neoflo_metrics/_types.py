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

Gauge thread-safety contract:
    Both _current_value mutation AND _instrument.add() are kept inside the lock.
    This prevents a race where two threads compute deltas against the same
    _current_value snapshot before either has called .add() — which would cause
    the UpDownCounter to drift from the true absolute value. OTEL instruments are
    documented as thread-safe for concurrent .add() calls, so holding the lock
    through the OTEL call is safe (no deadlock risk).
"""

from __future__ import annotations

import threading
from collections.abc import Mapping

from opentelemetry import metrics as otel_metrics

# Labels type uses Mapping (read-only view) instead of dict to signal that the
# SDK does not mutate the caller's labels dict, and to accept any mapping type.
Labels = Mapping[str, str] | None


class Counter:
    """Monotonically increasing counter. Use for totals (requests, errors, etc.)."""

    def __init__(self, instrument: otel_metrics.Counter) -> None:
        self._instrument = instrument

    def add(self, value: int | float, labels: Labels = None) -> None:
        """Increment the counter by value. Labels become OTEL Attributes."""
        self._instrument.add(value, attributes=dict(labels) if labels else {})


class Histogram:
    """Records distributions of values. Use for latencies, sizes, etc."""

    def __init__(self, instrument: otel_metrics.Histogram) -> None:
        self._instrument = instrument

    def record(self, value: int | float, labels: Labels = None) -> None:
        """Record a single observation. Labels become OTEL Attributes."""
        self._instrument.record(value, attributes=dict(labels) if labels else {})


class Gauge:
    """Tracks an absolute current value that can go up or down.

    Implemented over UpDownCounter with internal state tracking so that
    .set(42) translates to add(42 - current_value) on the underlying counter.

    Thread-safety: _lock protects both _current_value and _instrument.add()
    to prevent delta races between concurrent .set()/.add() calls in
    multi-threaded ASGI servers (uvicorn workers, etc.).
    """

    def __init__(self, instrument: otel_metrics.UpDownCounter) -> None:
        self._instrument = instrument
        self._current_value: float = 0.0
        # Lock must be held through both the value update AND the OTEL call.
        # See module docstring for the race condition this prevents.
        self._lock = threading.Lock()

    def set(self, value: int | float, labels: Labels = None) -> None:
        """Set the gauge to an absolute value."""
        attrs = dict(labels) if labels else {}
        with self._lock:
            delta = value - self._current_value
            self._current_value = float(value)
            # Inside the lock: prevents concurrent .set() calls from racing
            # on _current_value and producing an incorrect cumulative delta.
            self._instrument.add(delta, attributes=attrs)

    def add(self, value: int | float, labels: Labels = None) -> None:
        """Increment or decrement the gauge by a relative amount."""
        attrs = dict(labels) if labels else {}
        with self._lock:
            self._current_value += value
            self._instrument.add(value, attributes=attrs)
