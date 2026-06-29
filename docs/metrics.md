
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

| Domain | Metric | Type | SLA (p90 / p95 / p99) |
|--------|--------|------|----------------------|
| SQS | `sqs_messages_processed_total` | Counter | — |
| SQS | `sqs_message_processing_duration_ms` | Histogram | 5 s / 30 s / 120 s |
| SQS | `sqs_message_errors_total` | Counter | — |
| SQS | `sqs_queue_depth` | Gauge | — |
| SQS | `sqs_concurrent_workers` | Gauge | — |
| MongoDB | `mongodb_operation_duration_ms` | Histogram | 25 ms / 50 ms / 250 ms |
| MongoDB | `mongodb_operations_total` | Counter | — |
| MongoDB | `mongodb_errors_total` | Counter | — |
| Pipeline | `invoice_processing_total` | Counter | — |
| Pipeline | `invoice_processing_duration_ms` | Histogram | — |
| Pipeline | `invoice_pipeline_errors_total` | Counter | — |
| Claude AI | `claude_invocations_total` | Counter | — |
| Claude AI | `claude_invocation_duration_ms` | Histogram | 10 s / 30 s / 60 s |
| Claude AI | `claude_invocation_cost_usd_total` | Counter | — |
| Claude AI | `claude_tokens_used_total` | Counter | — |
| S3 | `s3_operations_total` | Counter | — |
| S3 | `s3_operation_duration_ms` | Histogram | — |
| N8N | `n8n_webhook_calls_total` | Counter | — |
| N8N | `n8n_webhook_duration_ms` | Histogram | — |

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

## Querying Percentiles (PromQL / Grafana)

Percentiles are derived from histogram bucket counts at query time — no separate metric is stored.
The SDK automatically merges SLA threshold values into `bucket_boundaries`, so `histogram_quantile()` returns exact (zero-interpolation-error) estimates at those points.

### HTTP request duration (auto via `MetricsMiddleware`)

```promql
# p95 per route
histogram_quantile(0.95,
  sum(rate(http_request_duration_ms_bucket[5m])) by (le, route, method)
)

# Alert: p95 > 500ms on any route for 5 minutes
histogram_quantile(0.95,
  sum(rate(http_request_duration_ms_bucket[5m])) by (le, route)
) > 500
```

### SQS message processing duration

```promql
# p90 per queue
histogram_quantile(0.90,
  sum(rate(sqs_message_processing_duration_ms_bucket[5m])) by (le, queue)
)

# SLA compliance rate — fraction of messages completing within 30s (p95 target)
sum(rate(sqs_message_processing_duration_ms_bucket{le="30000"}[5m])) by (queue)
/ sum(rate(sqs_message_processing_duration_ms_count[5m])) by (queue)

# Alert: p99 > 120s sustained for 10 minutes
histogram_quantile(0.99,
  sum(rate(sqs_message_processing_duration_ms_bucket[10m])) by (le, queue)
) > 120000
```

### MongoDB operation duration

```promql
# p95 per collection and operation type
histogram_quantile(0.95,
  sum(rate(mongodb_operation_duration_ms_bucket[5m])) by (le, collection, operation)
)

# Alert: p99 > 250ms for any collection
histogram_quantile(0.99,
  sum(rate(mongodb_operation_duration_ms_bucket[5m])) by (le, collection)
) > 250
```

### Claude AI invocation duration

```promql
# p90 — useful for capacity planning
histogram_quantile(0.90,
  sum(rate(claude_invocation_duration_ms_bucket[10m])) by (le, operation)
)

# Alert: p99 > 60s — Claude is approaching timeout
histogram_quantile(0.99,
  sum(rate(claude_invocation_duration_ms_bucket[5m])) by (le)
) > 60000
```

### SLA compliance dashboard pattern

For a single-value "SLA compliance %" panel in Grafana, divide the bucket at the SLA threshold
by the total count. Target ≥ 0.95 (95% compliance); alert when below 0.90 for 5 minutes.

```promql
# % of MongoDB ops completing within 50ms p95 SLA
sum(rate(mongodb_operation_duration_ms_bucket{le="50"}[5m]))
/ sum(rate(mongodb_operation_duration_ms_count[5m]))
```

### SLA constants reference

| Constant | p90 | p95 | p99 |
|----------|-----|-----|-----|
| `HTTP_SLA_MS` | 250 ms | 500 ms | 1000 ms |
| `SQS_SLA_MS` | 5 s | 30 s | 120 s |
| `MONGO_SLA_MS` | 25 ms | 50 ms | 250 ms |
| `CLAUDE_DURATION_SLA_MS` | 10 s | 30 s | 60 s |
| `CLAUDE_COST_SLA_USD` | $0.05 | $0.10 | $0.25 |
