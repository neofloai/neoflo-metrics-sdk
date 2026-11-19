"""
Per-tenant metric labelling for multi-tenant (SaaS) deployments.

On-prem deployments serve one tenant per process and identify it with the
process-level ``client`` resource attribute. In SaaS one process serves many
tenants, identified per request (``X-Tenant-ID`` header) or per queue message
(``tenant_id`` in the body). This module turns that into a ``tenant_id`` label.

Safety: tenant values come from client-controlled input, so each one is
(a) validated against a strict pattern and (b) counted against a hard cap of
distinct tenants. Past the cap, new values are labelled ``other`` — random
header values can never create unbounded Prometheus series.

Known limit: the first N distinct valid values win the slots, so a spoofed
value seen before a real tenant's first request can push that tenant into
``other``.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

TENANT_LABEL = "tenant_id"
MISSING_TENANT = "-"
OVERFLOW_TENANT = "other"
DEFAULT_MAX_TENANTS = 10

_VALID_TENANT = re.compile(r"^[a-zA-Z0-9\-_]{1,64}$")

_current: ContextVar[str | None] = ContextVar("neoflo_metrics_tenant", default=None)
_seen: set[str] = set()
_seen_lock = threading.Lock()


def _max_tenants() -> int:
    try:
        return max(1, int(os.environ.get("METRICS_MAX_TENANTS", DEFAULT_MAX_TENANTS)))
    except ValueError:
        return DEFAULT_MAX_TENANTS


def normalize_tenant(raw: object) -> str:
    """Return the label value for a raw tenant id: itself, ``-`` or ``other``."""
    if not isinstance(raw, str) or not _VALID_TENANT.match(raw):
        return MISSING_TENANT
    if raw in _seen:
        return raw
    with _seen_lock:
        if raw in _seen:
            return raw
        if len(_seen) >= _max_tenants():
            return OVERFLOW_TENANT
        _seen.add(raw)
        return raw


def set_tenant(raw: object) -> Token:
    """Set the current tenant for this context. Pass the result to reset_tenant()."""
    return _current.set(normalize_tenant(raw))


def reset_tenant(token: Token) -> None:
    _current.reset(token)


def get_tenant() -> str | None:
    """Return the current tenant label value, or None if none is set."""
    return _current.get()


@contextmanager
def tenant_context(raw: object) -> Iterator[None]:
    """Run a block with the given tenant as the current tenant."""
    token = set_tenant(raw)
    try:
        yield
    finally:
        reset_tenant(token)


def tenant_labels() -> dict[str, str]:
    """Labels to merge into a metric call: ``{tenant_id: ...}`` if a tenant is set."""
    tenant = _current.get()
    return {TENANT_LABEL: tenant} if tenant is not None else {}


def _reset_seen_for_tests() -> None:
    with _seen_lock:
        _seen.clear()
