"""Tests for MetricsMiddleware."""

import pytest


def test_import_without_starlette_does_not_crash(monkeypatch):
    """Importing neoflo_metrics must not raise even if starlette is missing."""
    import sys
    # Simulate starlette being absent by temporarily hiding the module.
    starlette_modules = {k: v for k, v in sys.modules.items() if "starlette" in k}
    for key in starlette_modules:
        sys.modules[key] = None  # type: ignore[assignment]
    try:
        # Re-importing the middleware module should not raise at module level.
        import importlib
        import neoflo_metrics.middleware as mw
        importlib.reload(mw)
        # Only instantiation should raise.
        with pytest.raises(RuntimeError, match="neoflo-metrics\\[starlette\\]"):
            mw.MetricsMiddleware(app=None)
    finally:
        for key in starlette_modules:
            sys.modules[key] = starlette_modules[key]


def test_client_and_server_errors_counted_separately(inmemory_sdk):
    """4xx and 5xx responses must increment separate counters."""
    from starlette.testclient import TestClient
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.responses import Response
    from neoflo_metrics.middleware import MetricsMiddleware

    def client_error(request):
        return Response(status_code=400)

    def server_error(request):
        return Response(status_code=500)

    app = Starlette(routes=[
        Route("/client", client_error),
        Route("/server", server_error),
    ])
    app.add_middleware(MetricsMiddleware)

    client = TestClient(app, raise_server_exceptions=False)
    client.get("/client")
    client.get("/server")

    metrics = inmemory_sdk.get_metrics_data()
    names = {
        m.name
        for rm in metrics.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
    }
    assert "http_request_client_errors_total" in names
    assert "http_request_server_errors_total" in names


def test_route_template_used_not_resolved_path(inmemory_sdk):
    """Metric labels must use /items/{id} not /items/42."""
    from starlette.testclient import TestClient
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.responses import Response
    from neoflo_metrics.middleware import MetricsMiddleware

    def get_item(request):
        return Response("ok")

    app = Starlette(routes=[Route("/items/{item_id}", get_item)])
    app.add_middleware(MetricsMiddleware)

    TestClient(app).get("/items/42")

    metrics = inmemory_sdk.get_metrics_data()
    routes = set()
    for rm in metrics.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == "http_requests_total":
                    for p in m.data.data_points:
                        routes.add(p.attributes.get("route", ""))

    assert "/items/{item_id}" in routes
    assert "/items/42" not in routes
