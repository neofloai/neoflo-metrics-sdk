"""
Configuration layer for the neoflo-metrics SDK.

This module owns the single global MetricsConfig instance that is set once
at process startup via configure_metrics() and then read by every other module.

WHY a global singleton instead of dependency injection:
    Metrics collection is infrastructure-level cross-cutting concern. Passing a
    config object through every call stack (service → business logic → metrics)
    would pollute every API boundary in the codebase. The singleton trades
    explicit coupling for zero-friction usage across 5+ microservices.

    The trade-off is acceptable because:
    1. configure_metrics() is called exactly once at startup (like logging.basicConfig).
    2. The config is immutable after initialization (MetricsConfig is a frozen dataclass).
    3. Tests can reset the global via set_config(None) between runs.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetricsConfig:
    """Immutable configuration snapshot for the metrics SDK.

    Frozen so that once configure_metrics() runs, no code path can silently
    mutate the config and cause different services to behave differently.
    """

    service_name: str
    otlp_endpoint: str
    environment: str = "production"
    export_interval_ms: int = 5000


# Module-level singleton — None until configure_metrics() is called.
_config: MetricsConfig | None = None


def set_config(cfg: MetricsConfig | None) -> None:
    """Store the global config. Accepts None to support test teardown."""
    global _config
    _config = cfg


def get_config() -> MetricsConfig:
    """Return the active config, raising if configure_metrics() was never called.

    WHY raise instead of returning a default:
        Silently using a default config (e.g. localhost endpoint) would cause
        metrics to silently disappear in production. Loud failure at first use
        makes misconfiguration obvious during integration testing.
    """
    if _config is None:
        raise RuntimeError(
            "neoflo_metrics is not configured. "
            "Call configure_metrics() before using any metrics instruments."
        )
    return _config
