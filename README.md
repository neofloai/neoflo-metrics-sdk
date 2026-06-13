# neoflo-metrics-sdk

Neoflo platform metrics SDK for Python microservices.

## Installation

```bash
pip install neoflo-metrics
```

## Usage

```python
from neoflo_metrics import configure_metrics, create_metrics
from neoflo_metrics.middleware import MetricsMiddleware

configure_metrics(
    service_name="invoice-validator-be",
    otlp_endpoint="http://otel-collector:4317",
    environment="production",
    export_interval_ms=5000,
)

app.add_middleware(MetricsMiddleware)

metrics = create_metrics({
    "invoices_processed_total": {
        "type": "counter",
        "description": "Total number of invoices successfully processed",
        "unit": "1",
    },
    "invoice_processing_duration_ms": {
        "type": "histogram",
        "description": "End-to-end invoice processing latency in milliseconds",
        "unit": "ms",
    },
    "invoices_pending": {
        "type": "gauge",
        "description": "Number of invoices currently waiting to be processed",
        "unit": "1",
    },
})

metrics.invoices_processed_total.add(1, {"vendor": "Acme", "status": "success"})
metrics.invoice_processing_duration_ms.record(71.8, {"stage": "extraction"})
metrics.invoices_pending.set(42)
```
