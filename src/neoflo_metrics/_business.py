"""
Business metrics factory for the neoflo-metrics SDK.

Services call create_metrics(spec) once at module load time and get back a
BusinessMetrics object whose attributes are typed metric instruments:

    metrics = create_metrics({"invoices_processed_total": {"type": "counter", ...}})
    metrics.invoices_processed_total.add(1, {"vendor": "Acme"})

WHY dynamic attribute setting (setattr) instead of code generation:
    Code generation (e.g., creating a typed class per service) would require
    either a build step or runtime class creation with metaclasses — both add
    significant complexity. Dynamic setattr gives the same dot-access ergonomics
    with far less machinery. The trade-off is that IDEs won't autocomplete
    instrument names, but that's acceptable for internal infrastructure code.

WHY validate type at creation time:
    Failing loudly at startup (when create_metrics() is called) rather than at
    the first .add() call makes misconfiguration obvious during integration
    testing and local development, not silently in production.

WHY MetricSpec TypedDict:
    Gives mypy and IDEs a precise schema for the spec dict without introducing
    a separate DSL or code generation step. total=False makes description and
    unit optional fields, matching the runtime defaults applied below.
"""

from __future__ import annotations

from typing import TypedDict

from ._provider import get_meter
from ._types import Counter, Gauge, Histogram

VALID_TYPES = frozenset({"counter", "histogram", "gauge"})


class MetricSpec(TypedDict, total=False):
    """Schema for a single metric entry in the create_metrics() spec dict.

    Attributes:
        type:        Required. One of "counter", "histogram", "gauge".
        description: Human-readable description of what this metric measures.
        unit:        OTEL unit string (e.g. "1", "ms", "By"). Defaults to "1".
    """

    type: str        # required — validated at runtime
    description: str
    unit: str


class BusinessMetrics:
    """Container for a service's business metric instruments.

    Attributes are set dynamically by create_metrics(). Each attribute is a
    Counter, Histogram, or Gauge instance with a stable, typed API.
    """

    pass


def create_metrics(spec: dict[str, MetricSpec]) -> BusinessMetrics:
    """Create and return a BusinessMetrics instance from a spec dict.

    Args:
        spec: Mapping of metric name → MetricSpec with keys:
              - type (required): "counter" | "histogram" | "gauge"
              - description (optional): human-readable description
              - unit (optional): OTEL unit string, defaults to "1"

    Returns:
        BusinessMetrics instance with one attribute per metric name.

    Raises:
        ValueError: If a metric type is not one of counter/histogram/gauge,
                    or if the "type" key is missing from a spec entry.
        RuntimeError: If configure_metrics() was not called before this.
    """
    meter = get_meter("neoflo.business")
    instance = BusinessMetrics()

    for name, meta in spec.items():
        metric_type = meta.get("type", "").lower()

        if not metric_type:
            raise ValueError(
                f"Missing 'type' key for metric '{name}'. "
                f"Must be one of: {', '.join(sorted(VALID_TYPES))}"
            )

        if metric_type not in VALID_TYPES:
            raise ValueError(
                f"Invalid metric type '{metric_type}' for '{name}'. "
                f"Must be one of: {', '.join(sorted(VALID_TYPES))}"
            )

        description = meta.get("description", "")
        unit = meta.get("unit", "1")

        if metric_type == "counter":
            otel_instrument = meter.create_counter(
                name=name,
                description=description,
                unit=unit,
            )
            instrument: Counter | Histogram | Gauge = Counter(otel_instrument)

        elif metric_type == "histogram":
            otel_instrument = meter.create_histogram(
                name=name,
                description=description,
                unit=unit,
            )
            instrument = Histogram(otel_instrument)

        else:  # gauge
            otel_instrument = meter.create_up_down_counter(
                name=name,
                description=description,
                unit=unit,
            )
            instrument = Gauge(otel_instrument)

        # setattr gives services `metrics.<name>.add(...)` without metaclasses.
        setattr(instance, name, instrument)

    return instance
