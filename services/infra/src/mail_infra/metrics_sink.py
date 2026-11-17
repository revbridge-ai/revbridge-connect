"""RevBridge Connect: where the deliverability jobs publish their metrics.

With CONNECT_METRICS_SINK=redis (the Connect setting), every run of an exporter (delivery_stats,
fleet_stats) stores a snapshot of its window in Redis, and the API serves all of them on
GET /metrics in Prometheus format (internal network only).

Key format: connect:metrics:<job> -> JSON {"job", "ts", "series": [...]}, where each series is
{"metric", "points": [[ts, value]], "type", "tags"}.
"""

from __future__ import annotations

import json
import os
import time

KEY_PREFIX = "connect:metrics:"


def sink_is_redis() -> bool:
    return os.getenv("CONNECT_METRICS_SINK", "").strip().lower() == "redis"


def publish(job: str, series: list[dict], ttl_sec: int = 3600) -> None:
    from mail_infra.core.settings import REDIS_URL
    from mail_infra.infrastructure.redis_utils import create_redis_client

    client = create_redis_client(REDIS_URL)
    try:
        payload = {"job": job, "ts": int(time.time()), "series": series}
        client.set(KEY_PREFIX + job, json.dumps(payload), ex=max(ttl_sec, 300))
    finally:
        client.close()
