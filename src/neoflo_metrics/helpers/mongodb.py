"""
MongoDB operation metrics helper.

Usage:
    from neoflo_metrics.helpers.mongodb import mongo_op_timer

    async with mongo_op_timer(metrics, operation="find", collection="invoices"):
        result = await self.collection.find_one({"_id": invoice_id})

The context manager records:
  - mongodb_operation_duration_ms  (histogram) on success
  - mongodb_operations_total       (counter, status=success) on success
  - mongodb_errors_total           (counter) on exception, then re-raises

The metrics argument must be a BusinessMetrics instance created via
create_metrics() with the above metric names declared.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any


@asynccontextmanager
async def mongo_op_timer(metrics: Any, operation: str, collection: str):
    """Async context manager that times a Motor MongoDB call.

    Args:
        metrics:    BusinessMetrics instance with mongodb_* instruments declared.
        operation:  Operation name used as a label (e.g. "find", "insert", "update").
        collection: Collection name used as a label (e.g. "invoices", "runs").
    """
    start = time.perf_counter()
    labels = {"operation": operation, "collection": collection}
    try:
        yield
        duration_ms = (time.perf_counter() - start) * 1000
        metrics.mongodb_operation_duration_ms.record(duration_ms, labels)
        metrics.mongodb_operations_total.add(1, {**labels, "status": "success"})
    except Exception:
        metrics.mongodb_errors_total.add(1, labels)
        raise
