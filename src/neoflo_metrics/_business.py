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

from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View

from ._provider import get_meter, get_provider
from ._types import Counter, Gauge, Histogram

VALID_TYPES = frozenset({"counter", "histogram", "gauge"})


def _merge_sla_boundaries(
    boundaries: list[float],
    sla: dict | None,
) -> list[float]:
    """Return a sorted, deduplicated boundary list that includes all SLA threshold values.

    OTel's histogram_quantile() is exact when the target percentile value is a
    bucket boundary. If 500ms is an SLA threshold but falls between two buckets
    (e.g. 250ms and 1000ms), the estimate is linearly interpolated and can be
    off by hundreds of milliseconds. Injecting the SLA value as a boundary
    eliminates that error at the threshold point.
    """
    if not sla:
        return boundaries
    extra = [v for v in sla.values() if v is not None]
    if not extra:
        return boundaries
    return sorted(set(boundaries) | set(extra))


class SLASpec(TypedDict, total=False):
    """SLA threshold values for histogram metrics, in the same unit as the histogram.

    Each declared value is automatically injected into bucket_boundaries so that
    histogram_quantile() estimates are exact at the threshold, not interpolated.
    Units match the parent metric (ms for duration histograms, USD for cost histograms).
    """

    p90: float
    p95: float
    p99: float


class MetricSpec(TypedDict, total=False):
    """Schema for a single metric entry in the create_metrics() spec dict.

    Attributes:
        type:              Required. One of "counter", "histogram", "gauge".
        description:       Human-readable description of what this metric measures.
        unit:              OTEL unit string (e.g. "1", "ms", "By"). Defaults to "1".
        bucket_boundaries: Custom histogram bucket edges. SLA threshold values are
                           automatically merged in if sla= is also provided.
        sla:               SLA thresholds in the same unit as the metric. When provided
                           alongside bucket_boundaries, threshold values are merged into
                           the bucket list for accurate histogram_quantile() estimates.
                           Use the pre-defined constants: SQS_SLA_MS, MONGO_SLA_MS, etc.
    """

    type: str        # required — validated at runtime
    description: str
    unit: str
    bucket_boundaries: list[float]
    sla: SLASpec


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
            bucket_boundaries = meta.get("bucket_boundaries")
            if bucket_boundaries:
                # Merge SLA threshold values into boundaries so histogram_quantile()
                # is exact at those points. If no sla= is given, boundaries are unchanged.
                effective_boundaries = _merge_sla_boundaries(
                    bucket_boundaries, meta.get("sla")
                )
                # Register a View so the MeterProvider uses these bucket edges
                # instead of the SDK default. Must be done before the instrument
                # is created so the View is in place when the first .record() fires.
                view = View(
                    instrument_name=name,
                    aggregation=ExplicitBucketHistogramAggregation(
                        boundaries=effective_boundaries
                    ),
                )
                get_provider().add_view(view)
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
