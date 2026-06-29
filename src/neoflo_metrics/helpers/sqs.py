"""
SQS message processing metrics helper.

Usage:
    from neoflo_metrics.helpers.sqs import sqs_handler_timer

    async with sqs_handler_timer(metrics, queue_name="init_queue"):
        await process_message(message_id, body)

The context manager records:
  - sqs_message_processing_duration_ms  (histogram) on success
  - sqs_messages_processed_total        (counter, status=success) on success
  - sqs_message_errors_total            (counter) on exception, then re-raises

The metrics argument must be a BusinessMetrics instance created via
create_metrics() with the above metric names declared.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any


@asynccontextmanager
async def sqs_handler_timer(metrics: Any, queue_name: str):
    """Async context manager that times an SQS message handler.

    Args:
        metrics:    BusinessMetrics instance with sqs_* instruments declared.
        queue_name: Short queue name used as a label (e.g. "init_queue").
    """
    start = time.perf_counter()
    try:
        yield
        duration_ms = (time.perf_counter() - start) * 1000
        metrics.sqs_message_processing_duration_ms.record(
            duration_ms, {"queue": queue_name}
        )
        metrics.sqs_messages_processed_total.add(
            1, {"queue": queue_name, "status": "success"}
        )
    except Exception:
        metrics.sqs_message_errors_total.add(1, {"queue": queue_name})
        raise
