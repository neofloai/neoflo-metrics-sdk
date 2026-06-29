# Task 5 — SDK Improvements

This document details every planned change to `neoflo-metrics-sdk` before instrumenting microservices.
Each item covers: what the current problem is, what we're changing, and what it unlocks in Grafana.

---

## Priority 1 — Critical Bugs (wrong data if not fixed)

---

### 1. Gauge label race — `_types.py:84`

**Problem**

`_current_value` is a single shared float on the `Gauge` object. When you call `.set()` with different label combinations, each call computes its delta against that one shared value — not against the previous value for that specific label.

```python
# current — one float for ALL label combinations
self._current_value: float = 0.0
```

So this sequence produces wrong data:
```python
metrics.sqs_queue_depth.set(10, {"queue": "init_queue"})
# delta = 10 - 0 = +10 → correct ✅

metrics.sqs_queue_depth.set(5, {"queue": "slow_tools_queue"})
# delta = 5 - 10 = -5 → wrong ❌  (should be +5)
# Grafana now shows slow_tools_queue at -5
```

**Fix**

Replace the single float with a per-label dict:

```python
self._values: dict[tuple, float] = {}

# in .set():
key = tuple(sorted((labels or {}).items()))
with self._lock:
    current = self._values.get(key, 0.0)
    delta = value - current
    self._values[key] = float(value)
    self._instrument.add(delta, attributes=attrs)
```

**Grafana impact**

Every gauge metric with label variations now shows its own correct independent value:

```promql
sqs_queue_depth{queue="init_queue"}        → 3  (actual)
sqs_queue_depth{queue="slow_tools_queue"}  → 47 (actual)
```

---

### 2. CPU percent first call returns 0.0 — `_infra.py:186`

**Problem**

`psutil.cpu_percent(interval=None)` computes CPU usage by comparing two snapshots over time. The very first call ever on a fresh `Process()` object has no prior snapshot — so it always returns `0.0` by design (documented psutil behavior).

The background thread calls `_collect_once()` immediately on startup (line 227), which means the first CPU data point pushed to the OTel collector is always `0.0` regardless of actual load.

```python
# current — no warm-up call
self._process = psutil.Process()
# ← first cpu_percent() call anywhere will return 0.0
```

**Fix**

One warm-up line in `__init__`, right after creating the `Process()`:

```python
self._process = psutil.Process()
self._process.cpu_percent(interval=None)  # primes internal baseline, discard the 0.0
```

**Grafana impact**

`process_cpu_usage_percent` is accurate from the first data point. No false `0%` reading at service startup.

```promql
process_cpu_usage_percent{service="invoice-validator-be"}  → accurate from t=0
```

---

## Priority 2 — Behavioral Gaps

---

### 3. Config validation — `_config.py:24`

**Problem**

`MetricsConfig` is a frozen dataclass with no validation. An empty `service_name` or a malformed `otlp_endpoint` silently passes and causes metrics to disappear without any error message.

```python
# current — accepts anything
@dataclass(frozen=True)
class MetricsConfig:
    service_name: str        # "" accepted silently
    otlp_endpoint: str       # "localhost" accepted silently
    export_interval_ms: int = 5000
```

**Fix**

Add `__post_init__` to validate at construction time:

```python
def __post_init__(self) -> None:
    if not self.service_name.strip():
        raise ValueError("service_name cannot be empty")
    if not (self.otlp_endpoint.startswith("http://") or self.otlp_endpoint.startswith("grpc://")):
        raise ValueError("otlp_endpoint must start with http:// or grpc://")
    if self.export_interval_ms < 1000:
        raise ValueError("export_interval_ms must be >= 1000ms")
```

**Grafana impact**

The `service` label is guaranteed to be non-empty on every metric. A misconfigured service now crashes loudly at startup rather than pushing metrics with `service=""` that are invisible in dashboards.

```promql
# Guaranteed after fix — never shows up as service=""
http_requests_total{service="invoice-validator-be"}
```

---

### 4. Starlette optional import — `middleware.py:59`

**Problem**

`starlette` is listed as an optional dependency in `pyproject.toml`, but it's imported unconditionally at the top of `middleware.py`. This means any service that does `from neoflo_metrics import configure_metrics` without starlette installed will crash with `ModuleNotFoundError` — even if they never use the middleware.

```python
# middleware.py:59 — runs at import time, always
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
```

**Fix**

Guard the import and raise a clear error only if middleware is actually instantiated:

```python
try:
    from starlette.middleware.base import BaseHTTPMiddleware
    _starlette_available = True
except ImportError:
    _starlette_available = False

class MetricsMiddleware:
    def __init__(self):
        if not _starlette_available:
            raise RuntimeError(
                "MetricsMiddleware requires starlette. "
                "Install it with: pip install neoflo-metrics[starlette]"
            )
```

**Grafana impact**

No direct metrics change. This allows non-Starlette Python services to import the SDK without crashing.

---

### 5. 4xx vs 5xx error split — `middleware.py:112` + `_infra.py`

**Problem**

The current `errors_total` counter fires for anything `>= 400`. A spike in this counter could mean normal client-side validation errors (400s) or a real server incident (500s) — there's no way to tell them apart. Alerting on `errors_total` is therefore too noisy to be useful.

```python
# middleware.py:112 — both 4xx and 5xx go into the same bucket
if response.status_code >= 400:
    instruments["errors_total"].add(1, attributes=labels)
```

**Fix**

Add two new counters in `get_http_instruments()` and split the check:

```python
# new counters added to _infra.py get_http_instruments()
"client_errors_total": meter.create_counter("http_request_client_errors_total"),
"server_errors_total": meter.create_counter("http_request_server_errors_total"),

# middleware.py — replace the single check
if 400 <= response.status_code < 500:
    instruments["client_errors_total"].add(1, attributes=labels)
elif response.status_code >= 500:
    instruments["server_errors_total"].add(1, attributes=labels)
```

The existing `errors_total` (>= 400) is kept for backward compatibility.

**Grafana impact**

Two separate panels and separate alert rules become possible:

```promql
# Server errors — alert if rate > 2% sustained for 5 minutes
rate(http_request_server_errors_total{service="invoice-validator-be"}[5m])
/ rate(http_requests_total{service="invoice-validator-be"}[5m])

# Client errors — informational only, no alert
rate(http_request_client_errors_total{service="invoice-validator-be"}[5m])
```

---

### 6. Per-histogram bucket boundaries — `_business.py` + `_provider.py`

**Problem**

The only histogram bucket set is hardcoded for HTTP latency: `[5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000]` (max 5 seconds). Business metrics have very different measurement ranges:

- SQS `slow_tools_queue` handlers (line item matching): up to **5 minutes**
- MongoDB operations: as low as **1ms**
- Claude AI invocation cost: **$0.001 – $0.5**

Any value beyond the last bucket goes into `+Inf`. p95/p99 queries over these metrics return `+Inf`, which is useless.

Currently there's no way to specify custom buckets when calling `create_metrics()`:

```python
# current MetricSpec — no bucket support
class MetricSpec(TypedDict, total=False):
    type: str
    description: str
    unit: str
```

**Fix**

Add `bucket_boundaries` as an optional key in `MetricSpec`:

```python
class MetricSpec(TypedDict, total=False):
    type: str
    description: str
    unit: str
    bucket_boundaries: list[float]  # new
```

When provided for a histogram, `create_metrics()` registers an OTel `View` on the `MeterProvider` with those boundaries.

Add pre-defined named constants to `_provider.py` and export them from `__init__.py`:

```python
SQS_PROCESSING_BOUNDARIES_MS = [100, 250, 500, 1000, 5000, 15000, 30000, 60000, 120000, 300000]
MONGO_DURATION_BOUNDARIES_MS = [1, 5, 10, 25, 50, 100, 250, 500, 1000]
CLAUDE_COST_BOUNDARIES_USD   = [0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0]
```

Usage in a service:

```python
from neoflo_metrics import create_metrics, SQS_PROCESSING_BOUNDARIES_MS

metrics = create_metrics({
    "sqs_message_processing_duration_ms": {
        "type": "histogram",
        "unit": "ms",
        "bucket_boundaries": SQS_PROCESSING_BOUNDARIES_MS,
    }
})
```

**Grafana impact**

p95 and p99 queries now return real numbers instead of `+Inf`:

```promql
# p95 SQS processing time — meaningful up to 300 seconds
histogram_quantile(0.95,
  sum(rate(sqs_message_processing_duration_ms_bucket[5m])) by (le, queue)
)
# → slow_tools_queue: ~45000ms, fast_tools_queue: ~4200ms

# p95 MongoDB latency — meaningful down to 1ms
histogram_quantile(0.95,
  sum(rate(mongodb_operation_duration_ms_bucket[5m])) by (le, collection, operation)
)
# → invoices/find: ~12ms, runs/update: ~8ms
```

---

### 7. ASGI lifespan shutdown — `__init__.py`

**Problem**

`SystemMetricsCollector` runs as a daemon thread with no way to stop it cleanly. There's no hook for FastAPI's lifespan shutdown, so:
- In tests: the thread leaks between test runs
- On ECS container shutdown (SIGTERM): any in-flight metric export in the last ~30s is dropped silently

**Fix**

Store the collector reference and expose a `shutdown_metrics()` function:

```python
# __init__.py
_collector: SystemMetricsCollector | None = None

def shutdown_metrics() -> None:
    """Stop background metrics collection. Call from ASGI lifespan shutdown."""
    if _collector is not None:
        _collector.stop()
```

Usage in FastAPI lifespan:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_metrics("invoice-validator-be", "http://otel-collector:4317")
    yield
    shutdown_metrics()
```

**Grafana impact**

No new metrics. Prevents a silent gap in CPU/memory/uptime readings during rolling deploys — the last data points before shutdown are flushed cleanly instead of being dropped.

---

## Priority 3 — New Helpers

---

### 8. SQS helper — `src/neoflo_metrics/helpers/sqs.py`

**What we're adding**

All three services use SQS. The duration timing logic already exists in `invoice-validator-be/src/services/aws/sqs.py:116–133` — it just logs the result, not push it to metrics. The helper captures that pattern as a reusable async context manager.

```python
# helpers/sqs.py
@asynccontextmanager
async def sqs_handler_timer(metrics, queue_name: str):
    start = time.perf_counter()
    try:
        yield
        duration_ms = (time.perf_counter() - start) * 1000
        metrics.sqs_message_processing_duration_ms.record(duration_ms, {"queue": queue_name})
        metrics.sqs_messages_processed_total.add(1, {"queue": queue_name, "status": "success"})
    except Exception:
        metrics.sqs_message_errors_total.add(1, {"queue": queue_name})
        raise
```

Applied in `invoice-validator-be/src/services/aws/sqs.py` inside `handle_message` (line 114):

```python
async with sqs_handler_timer(sqs_metrics, queue_name=self.queue_name):
    status = await message_handler(message_id, parsed_body)
```

One change covers all three queues (`init_queue`, `fast_tools_queue`, `slow_tools_queue`) since `self.queue_name` becomes the label automatically.

**Grafana impact**

```promql
# p95 processing time per queue
histogram_quantile(0.95,
  sum(rate(sqs_message_processing_duration_ms_bucket[5m])) by (le, queue)
)
# → slow_tools_queue: ~78s, fast_tools_queue: ~4s, init_queue: ~0.6s

# Error rate per queue — alert if sustained above threshold
rate(sqs_message_errors_total[5m]) by (queue)

# Throughput per queue
rate(sqs_messages_processed_total{status="success"}[5m]) by (queue)
```

---

### 9. MongoDB helper — `src/neoflo_metrics/helpers/mongodb.py`

**What we're adding**

All three services use Motor for MongoDB but no query latency is tracked anywhere. The helper wraps Motor calls in an async context manager using the same pattern as the SQS helper.

```python
# helpers/mongodb.py
@asynccontextmanager
async def mongo_op_timer(metrics, operation: str, collection: str):
    start = time.perf_counter()
    try:
        yield
        duration_ms = (time.perf_counter() - start) * 1000
        metrics.mongodb_operation_duration_ms.record(
            duration_ms, {"operation": operation, "collection": collection}
        )
        metrics.mongodb_operations_total.add(
            1, {"operation": operation, "collection": collection, "status": "success"}
        )
    except Exception:
        metrics.mongodb_errors_total.add(
            1, {"operation": operation, "collection": collection}
        )
        raise
```

Applied at DB call sites, e.g. in `invoice-validator-be`:

```python
async with mongo_op_timer(db_metrics, operation="find", collection="invoices"):
    result = await self.collection.find_one({"_id": invoice_id})
```

**Grafana impact**

```promql
# p95 MongoDB latency per collection and operation
histogram_quantile(0.95,
  sum(rate(mongodb_operation_duration_ms_bucket[5m])) by (le, collection, operation)
)
# → invoices/find: ~12ms, runs/update: ~8ms, costing/insert: ~5ms

# MongoDB error rate — alert if non-zero for > 2 minutes
rate(mongodb_errors_total[5m]) by (collection)
```

Both helpers are exported from `src/neoflo_metrics/helpers/__init__.py`.

---

## Files Changed Summary

| File | Type | What changes |
|------|------|--------------|
| `src/neoflo_metrics/_types.py` | Modify | Gauge: `_current_value` → `_values` dict per label |
| `src/neoflo_metrics/_infra.py` | Modify | CPU warm-up call; add `client_errors_total`, `server_errors_total` |
| `src/neoflo_metrics/_config.py` | Modify | `__post_init__` validation |
| `src/neoflo_metrics/middleware.py` | Modify | Lazy starlette import guard; split 4xx/5xx counters |
| `src/neoflo_metrics/_provider.py` | Modify | `get_provider()`, boundary constants, View registration helper |
| `src/neoflo_metrics/_business.py` | Modify | `bucket_boundaries` in `MetricSpec`; register View per histogram |
| `src/neoflo_metrics/__init__.py` | Modify | `shutdown_metrics()`; export boundary constants |
| `src/neoflo_metrics/helpers/__init__.py` | Create | Export `sqs_handler_timer`, `mongo_op_timer` |
| `src/neoflo_metrics/helpers/sqs.py` | Create | `sqs_handler_timer` context manager |
| `src/neoflo_metrics/helpers/mongodb.py` | Create | `mongo_op_timer` context manager |
| `pyproject.toml` | Modify | Add `[dev]` optional dependencies: `pytest`, `pytest-asyncio` |

---

## Tests to Write — `tests/`

Uses OTel's `InMemoryMetricReader` — no real collector needed.

| File | Covers |
|------|--------|
| `tests/conftest.py` | SDK init/teardown fixtures (reset `_initialized`, `_meter_provider`) |
| `tests/test_types.py` | Gauge multi-label isolation; concurrent `.set()` thread safety |
| `tests/test_config.py` | Empty `service_name` raises; bad endpoint raises; short interval raises |
| `tests/test_middleware.py` | No crash without starlette; 4xx/5xx counted separately; route template used |
| `tests/test_helpers.py` | SQS timer on success; SQS timer on exception; MongoDB timer duration |
| `tests/test_system_collector.py` | CPU warm-up: second reading is > 0 |

---

## All Metrics — Complete List

### Auto — Via `MetricsMiddleware` (all 3 services, zero code needed)

| Metric | Type |
|--------|------|
| `http_request_duration_ms` | Histogram |
| `http_requests_total` | Counter |
| `http_requests_in_flight` | Gauge |
| `http_request_client_errors_total` | Counter (4xx) |
| `http_request_server_errors_total` | Counter (5xx) |

### Auto — Via `configure_metrics()` (all 3 services, zero code needed)

| Metric | Type |
|--------|------|
| `process_cpu_usage_percent` | Gauge |
| `process_memory_bytes` | Gauge |
| `process_uptime_seconds` | Counter |

### invoice-validator-be — Manual via `create_metrics()`

| Domain | Metrics |
|--------|---------|
| SQS | `sqs_messages_processed_total`, `sqs_message_processing_duration_ms`, `sqs_message_errors_total`, `sqs_queue_depth`, `sqs_concurrent_workers` |
| MongoDB | `mongodb_operation_duration_ms`, `mongodb_operations_total`, `mongodb_errors_total` |
| Pipeline | `invoice_processing_total`, `invoice_processing_duration_ms`, `invoice_pipeline_errors_total` |
| Claude AI | `claude_invocations_total`, `claude_invocation_duration_ms`, `claude_invocation_cost_usd_total`, `claude_tokens_used_total` |
| S3 | `s3_operations_total`, `s3_operation_duration_ms` |
| N8N | `n8n_webhook_calls_total`, `n8n_webhook_duration_ms` |

### file-ingestion-service — Manual via `create_metrics()`

| Sub-service | Metrics |
|-------------|---------|
| Scheduler | `scheduler_cycles_total`, `scheduler_tenants_processed`, `ingestion_jobs_created_total`, `ingestion_jobs_skipped_total`, `email_poll_duration_ms`, `email_poll_results_total` |
| Worker | `sqs_messages_processed_total`, `sqs_message_processing_duration_ms`, `sqs_message_errors_total`, `document_ingestion_total`, `document_ingestion_duration_ms`, `s3_events_processed_total`, `external_api_calls_total`, `external_api_duration_ms` |

### ums-rbac — Manual via `create_metrics()`

| Domain | Metrics |
|--------|---------|
| Auth | `auth_login_total`, `auth_login_duration_ms`, `auth_refresh_total`, `auth_logout_total` |
| Access check | `access_check_total`, `access_check_duration_ms`, `access_verify_total`, `access_verify_duration_ms` |
| MongoDB | `mongodb_operation_duration_ms`, `mongodb_operations_total` |
| Other | `active_sessions`, `jwt_validation_errors_total` |

**Total: ~50 metrics** across all services (8 auto + ~42 manual).

---

## How Each Metric Gets Implemented

### Auto — HTTP Middleware

| Metric | How it's calculated |
|--------|---------------------|
| `http_request_duration_ms` | `perf_counter()` recorded before `await call_next(request)`, diff taken after response returns |
| `http_requests_total` | Incremented once per response, labeled with `route` + `method` + `status_code` |
| `http_requests_in_flight` | `+1` on request enter, `-1` on response exit |
| `http_request_client_errors_total` | Incremented when `400 <= status_code < 500` |
| `http_request_server_errors_total` | Incremented when `status_code >= 500` |

### Auto — System (background thread every 30s)

| Metric | How it's calculated |
|--------|---------------------|
| `process_cpu_usage_percent` | `psutil.cpu_percent()` — OS-level CPU time used by this process since last call |
| `process_memory_bytes` | `psutil.memory_info().rss` — RAM currently held by the process |
| `process_uptime_seconds` | `time.time() - process_start_time`, delta added each collection cycle |

### invoice-validator-be

**SQS** — wrap `handle_message()` in `sqs.py` with `sqs_handler_timer`

| Metric | How it's calculated |
|--------|---------------------|
| `sqs_message_processing_duration_ms` | `perf_counter()` at start of `handle_message()` → when handler returns. Full handler time start to end. |
| `sqs_messages_processed_total` | Incremented once per message that completes without exception, labeled with `queue` name |
| `sqs_message_errors_total` | Incremented when `handle_message()` raises an exception |
| `sqs_queue_depth` | Periodically call `SQS.get_queue_attributes(ApproximateNumberOfMessages)`, `.set()` the returned value |
| `sqs_concurrent_workers` | `+1` when semaphore acquired in `_semaphore_wrapper`, `-1` when released |

**MongoDB** — wrap Motor calls with `mongo_op_timer`

| Metric | How it's calculated |
|--------|---------------------|
| `mongodb_operation_duration_ms` | `perf_counter()` before Motor call (`.find_one()`, `.insert_one()`, etc.) → after `await` returns |
| `mongodb_operations_total` | Incremented per Motor call, labeled with `operation` (find/insert/update) + `collection` |
| `mongodb_errors_total` | Incremented when Motor call raises an exception |

**Invoice Pipeline** — wrap each stage in the processing pipeline

| Metric | How it's calculated |
|--------|---------------------|
| `invoice_processing_total` | Incremented when the full pipeline (OCR → extraction → matching → bill post) completes or fails |
| `invoice_processing_duration_ms` | `perf_counter()` at start of each stage → end of that stage, labeled with `stage` (ocr/extraction/matching/validation) |
| `invoice_pipeline_errors_total` | Incremented when a specific stage raises, labeled with which `stage` failed |

**Claude AI** — wrap Claude agent calls in the matching engine

| Metric | How it's calculated |
|--------|---------------------|
| `claude_invocations_total` | Incremented once per Claude API call, labeled with `operation` |
| `claude_invocation_duration_ms` | `perf_counter()` before Claude agent call → after response received |
| `claude_invocation_cost_usd_total` | `.add(cost)` where `cost` comes from Claude's usage response — already tracked in `MatchingMetrics` |
| `claude_tokens_used_total` | `.add(token_count)` from Claude's usage response, labeled `input` or `output` |

**S3** — wrap boto3 S3 calls

| Metric | How it's calculated |
|--------|---------------------|
| `s3_operations_total` | Incremented per boto3 S3 call, labeled with `operation` (presigned_url/upload/download) |
| `s3_operation_duration_ms` | `perf_counter()` before boto3 call → after it returns |

**N8N** — wrap outbound webhook HTTP calls

| Metric | How it's calculated |
|--------|---------------------|
| `n8n_webhook_calls_total` | Incremented per outbound webhook call, labeled success/failed/timeout |
| `n8n_webhook_duration_ms` | `perf_counter()` before `httpx.post()` → after response |

### file-ingestion-service

**Scheduler** — instrument the scheduler loop and external email API calls

| Metric | How it's calculated |
|--------|---------------------|
| `scheduler_cycles_total` | Incremented once per scheduler loop iteration |
| `scheduler_tenants_processed` | Incremented per tenant evaluated in a cycle |
| `ingestion_jobs_created_total` | Incremented each time an SQS job is dispatched, labeled with `source` (gmail/outlook/freshdesk) |
| `ingestion_jobs_skipped_total` | Incremented when a tenant is skipped (already running / schedule not matched) |
| `email_poll_duration_ms` | `perf_counter()` before Gmail/Outlook API call → after response |
| `email_poll_results_total` | Incremented per poll, labeled with `source` + `status` (success/error) |

**Worker** — wrap SQS handler and document ingestion flow

| Metric | How it's calculated |
|--------|---------------------|
| `sqs_message_processing_duration_ms` | Same as invoice-validator-be — start of `handle_message` to end |
| `document_ingestion_total` | Incremented when a document is fully stored in MongoDB after processing |
| `document_ingestion_duration_ms` | `perf_counter()` from S3 event received → document record saved in MongoDB |
| `s3_events_processed_total` | Incremented per S3 event notification handled by the worker |
| `external_api_calls_total` | Incremented per outbound call to Gmail/Outlook/Freshdesk |
| `external_api_duration_ms` | `perf_counter()` before external HTTP call → after response |

### ums-rbac

| Metric | How it's calculated |
|--------|---------------------|
| `auth_login_total` | Incremented per login attempt, labeled `status=success/failure` + `failure_reason` |
| `auth_login_duration_ms` | `perf_counter()` at start of login handler → after JWT issued (or error returned) |
| `auth_refresh_total` | Incremented per token refresh attempt |
| `auth_logout_total` | Incremented per logout call |
| `access_check_total` | Incremented per `/access/check` call, labeled `decision=allow/deny` — called on every authenticated request across all services |
| `access_check_duration_ms` | `perf_counter()` at start of access check handler → after RBAC engine returns allow/deny |
| `access_verify_total` | Incremented per `/access/verify` call (JWT decode + access check combined) |
| `access_verify_duration_ms` | `perf_counter()` wrapping the full verify endpoint |
| `active_sessions` | `.set(count)` where count = `db.sessions.count_documents({active: true})`, queried periodically |
| `jwt_validation_errors_total` | Incremented when JWT decode fails, labeled with `error_type` (expired/invalid_signature/missing_sub) |
| `mongodb_operation_duration_ms` | `perf_counter()` wrapping each Motor call for user/grant/session queries |
| `mongodb_operations_total` | Incremented per Motor call with `operation` + `collection` labels |
