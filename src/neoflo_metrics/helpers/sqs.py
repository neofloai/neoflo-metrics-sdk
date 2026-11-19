"""
SQS message processing metrics helper.

Usage:
    from neoflo_metrics.helpers.sqs import sqs_handler_timer

    async with sqs_handler_timer(metrics, queue_name="init_queue",
                                 tenant_id=body.get("tenant_id")):
        await process_message(message_id, body)

The context manager records:
  - sqs_message_processing_duration_ms  (histogram) on success
  - sqs_messages_processed_total        (counter, status=success) on success
  - sqs_message_errors_total            (counter) on exception, then re-raises

When tenant_id is given, every metric recorded inside the block (these three
and any business metric the handler emits) carries a tenant_id label. The value
is validated and capped by neoflo_metrics._tenant. Omit it for single-tenant
services and the labels are unchanged.

The metrics argument must be a BusinessMetrics instance created via
create_metrics() with the above metric names declared.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any

from .._tenant import reset_tenant, set_tenant


@asynccontextmanager
async def sqs_handler_timer(metrics: Any, queue_name: str, tenant_id: Any = None):
    """Async context manager that times an SQS message handler.

    Args:
        metrics:    BusinessMetrics instance with sqs_* instruments declared.
        queue_name: Short queue name used as a label (e.g. "init_queue").
        tenant_id:  Tenant from the message body. None leaves labels unchanged.
    """
    token = set_tenant(tenant_id) if tenant_id is not None else None
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
    finally:
        if token is not None:
            reset_tenant(token)
