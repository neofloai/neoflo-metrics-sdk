"""
Starlette/FastAPI middleware for automatic HTTP metrics collection.

Add to a FastAPI app with:

    app.add_middleware(MetricsMiddleware)

This provides Layer 1 infrastructure metrics (http_request_duration_ms,
http_requests_total, http_requests_in_flight, http_request_errors_total)
with zero instrumentation effort from the service author.

Design decisions:

WHY perf_counter for timing:
    time.time() uses the wall clock, which can jump backwards on NTP
    adjustments or leap seconds, producing negative durations. perf_counter()
    is monotonic and has nanosecond resolution — the right tool for latency
    measurement.

WHY exception handling wraps ONLY the metrics code:
    The fundamental contract of this middleware is "metrics collection must
    never break the application". If we wrapped `await call_next(request)` in
    the same try/except, a metrics failure would silently swallow request
    errors, making debugging extremely difficult. The structure is:

        instruments, base_labels = None, {}   # sentinels — always defined
        try:
            [metrics pre-request]
        except:
            instruments = None  # disable post-request recording
        response = await call_next(request)   # <-- never inside a swallowing except
        try:
            [metrics post-request, guarded by instruments is not None]
        except:
            pass
        return response

WHY extract route from scope["route"]:
    request.url.path gives the full path with path parameters resolved
    (e.g., /users/123). Using the route template (/users/{user_id}) gives
    bounded cardinality in metric labels — critical for time-series databases
    that struggle with high-cardinality label values.

WHY sentinels are declared before the try block:
    Declaring `instruments: dict | None = None` and `base_labels: dict = {}`
    before the try block ensures they are always defined at the post-request
    guard check, regardless of where inside the try block an exception fires.
    Relying on assignment inside the except block alone leaves the variables
    undefined if the exception fires before the assignment is reached.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from ._infra import get_http_instruments

logger = logging.getLogger(__name__)

# Type alias for the call_next callable passed by Starlette's middleware chain.
RequestResponseEndpoint = Callable[[Request], Awaitable[Response]]


class MetricsMiddleware(BaseHTTPMiddleware):
    """Automatic HTTP metrics middleware for Starlette/FastAPI."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Resolve route template early; fall back to raw path if routing hasn't
        # matched yet (e.g., 404 for unknown paths).
        route = _extract_route(request)
        method = request.method

        # Sentinel defaults — declared before any try block so they are always
        # defined at the post-request guard, regardless of where an exception fires.
        instruments: dict[str, Any] | None = None
        base_labels: dict[str, str] = {}

        # --- Pre-request metrics (best-effort, never crash the app) ---
        try:
            instruments = get_http_instruments()
            common = instruments["_common_labels"]
            base_labels = {**common, "route": route, "method": method}
            instruments["in_flight"].add(1, attributes=base_labels)
        except Exception:
            logger.exception("MetricsMiddleware: failed to record pre-request metrics")
            instruments = None  # Disable post-request metrics too.

        # --- Actual request handling — MUST NOT be inside a metrics try/except ---
        # perf_counter gives monotonic, high-resolution timing unaffected by NTP.
        start = time.perf_counter()
        response: Response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000

        # --- Post-request metrics (best-effort) ---
        if instruments is not None:
            try:
                status_code = str(response.status_code)
                labels = {**base_labels, "status_code": status_code}

                instruments["in_flight"].add(-1, attributes=base_labels)
                instruments["duration"].record(duration_ms, attributes=labels)
                instruments["requests_total"].add(1, attributes=labels)

                if response.status_code >= 400:
                    instruments["errors_total"].add(1, attributes=labels)

            except Exception:
                logger.exception("MetricsMiddleware: failed to record post-request metrics")

        return response


def _extract_route(request: Request) -> str:
    """Return the route template string, falling back to the raw URL path.

    Using the template (/items/{item_id}) instead of the resolved path
    (/items/42) bounds metric cardinality to the number of routes, not
    the number of unique parameter values.
    """
    route = request.scope.get("route")
    if isinstance(route, Route):
        return route.path
    return request.url.path
