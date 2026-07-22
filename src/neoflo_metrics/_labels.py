"""
Label hygiene for metric attributes (security fix: 01-H1, denylist only).

WHY THIS FILE EXISTS
--------------------
Metric labels become Prometheus label values: they are stored, queryable by
anyone with Prometheus/Grafana read access, and shipped through the OTel
collector. Sensitive values (passwords, tokens, emails, user ids, ...) must
never land there — a privacy / secret-exposure problem.

Previously the SDK forwarded whatever labels a caller passed, with no guard —
so a single well-meaning `metrics.x.add(1, {"email": e})` in any of 5+ services
would leak. This module enforces a **denylist** centrally so no service can make
that mistake: any label KEY whose name looks sensitive (matches a fragment in
``_DENY_SUBSTRINGS``) is dropped before the label reaches the backend.

WHY DENYLIST ONLY (no allowlist):
    An allowlist was considered but rejected — it is fragile and gives false
    confidence: (a) some labels (e.g. ``client``) arrive as OTel *resource
    attributes* and never pass through this code at all, so an allowlist can't
    see them; and (b) a future engineer adding a legitimate new label would have
    their data silently dropped unless they also knew to edit a list in this SDK.
    A denylist only removes things that are clearly sensitive and lets every
    other (legitimate, low-cardinality) label through — safe and maintainable.

Only key NAMES are inspected (not values). Dropped keys are logged once per key
(never per call — the record path stays cheap). High-cardinality *values* (e.g.
raw request paths in the ``route`` label) are a separate concern handled in
middleware (`_extract_route`), not here.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

# Sensitive key-name fragments (case-insensitive substring match). A label whose
# key contains any of these is dropped. Kept high-signal to avoid dropping
# legitimate labels — these fragments do not appear in any known platform label.
_DENY_SUBSTRINGS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "apikey",
        "api_key",
        "authorization",
        "auth_",
        "credential",
        "cookie",
        "session",
        "email",
        "ssn",
        "username",
        "user_id",
        "userid",
        "invoice_id",
        "document_id",
        "request_id",
        "cvv",
    }
)

# Warn-once bookkeeping so a mislabelled hot path doesn't flood the logs.
_warned: set[str] = set()
_warned_lock = threading.Lock()


def _warn_once(key: str) -> None:
    with _warned_lock:
        if key in _warned:
            return
        _warned.add(key)
    logger.warning(
        "neoflo-metrics: dropped metric label %r — its name matches a sensitive "
        "pattern and must not be used as a metric label (labels are stored and "
        "queryable). Log sensitive values via the logger instead.",
        key,
    )


def _is_denied(lowered_key: str) -> bool:
    return any(fragment in lowered_key for fragment in _DENY_SUBSTRINGS)


def sanitize_labels(labels: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a NEW label dict with sensitive-named keys removed.

    Only keys matching the denylist are dropped; every other label passes
    through unchanged. Never mutates the caller's mapping. Empty/None -> {}.
    """
    if not labels:
        return {}
    clean: dict[str, Any] = {}
    for key, value in labels.items():
        lowered = key.lower() if isinstance(key, str) else str(key).lower()
        if _is_denied(lowered):
            _warn_once(str(key))
            continue
        clean[key] = value
    return clean
