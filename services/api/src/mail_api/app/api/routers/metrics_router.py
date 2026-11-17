"""RevBridge Connect: GET /metrics in the Prometheus exposition format.

The deliverability jobs (delivery_stats, fleet_stats) write a snapshot of the last window
to Redis (mail_infra.metrics_sink). This endpoint converts it to Prometheus text:
  mail.delivery.events  -> connect_mail_delivery_events
  tags "key:value"      -> labels {key="value"}
Every series is a gauge (value of the last window). No authentication: Caddy blocks
/metrics on the public hosts; Prometheus scrapes api:8001 directly on the internal network.
"""

from __future__ import annotations

import json
import os
import re

import redis
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

metrics_router = APIRouter(tags=["metrics"])
KEY_PREFIX = "connect:metrics:"
_NAME_RE = re.compile(r"[^a-zA-Z0-9_]")


def _metric_name(name: str) -> str:
    return "connect_" + _NAME_RE.sub("_", name).strip("_")


def _labels(tags: list[str], job: str) -> str:
    pairs = {"job": job}
    for t in tags or []:
        k, _, v = t.partition(":")
        k = _NAME_RE.sub("_", k).strip("_")
        if k and k not in pairs:
            pairs[k] = v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return "{" + ",".join(f'{k}="{v}"' for k, v in sorted(pairs.items())) + "}"


def _live_metrics(client) -> list[str]:
    """Metrics measured on the spot (queue, ledger, suppressions). Each block fails on its own."""
    from mail_shared.db.mysql_sync import fetch_all

    out: list[str] = []
    try:
        prefix = os.getenv("EMAIL_QUEUE_PREFIX", "e")
        main_key = os.getenv("EMAIL_QUEUE_KEY", f"{prefix}:queue")
        depth = client.llen(main_key)
        per_customer = 0
        for key in client.scan_iter(match=os.getenv("EMAIL_CUSTOMER_QUEUE_PREFIX", f"{prefix}:q:c:") + "*", count=200):
            per_customer += client.llen(key)
        out += ["# TYPE connect_queue_depth gauge",
                f'connect_queue_depth{{queue="main"}} {depth}',
                f'connect_queue_depth{{queue="per_customer"}} {per_customer}']
    except Exception:  # noqa: BLE001
        pass
    try:
        rows = fetch_all("SELECT state, COUNT(*) AS n FROM send_ledger "
                         "WHERE created_at >= NOW() - INTERVAL 24 HOUR GROUP BY state")
        out.append("# TYPE connect_send_ledger_24h gauge")
        out += [f'connect_send_ledger_24h{{state="{r["state"]}"}} {int(r["n"])}' for r in rows]
    except Exception:  # noqa: BLE001
        pass
    try:
        rows = fetch_all("SELECT suppression_type, COUNT(*) AS n FROM suppressions "
                         "WHERE is_active = 1 GROUP BY suppression_type")
        out.append("# TYPE connect_suppressions_active gauge")
        out += [f'connect_suppressions_active{{type="{r["suppression_type"]}"}} {int(r["n"])}' for r in rows]
    except Exception:  # noqa: BLE001
        pass
    try:
        rows = fetch_all("SELECT r.name, s.state FROM connect_node_state s JOIN relays r ON r.id = s.relay_id")
        out.append("# TYPE connect_node_state gauge")
        out += [f'connect_node_state{{node="{r["name"]}",state="{r["state"]}"}} 1' for r in rows]
    except Exception:  # noqa: BLE001
        pass
    return out


@metrics_router.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
async def metrics() -> str:
    client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)
    try:
        lines: list[str] = _live_metrics(client)
        seen_types: set[str] = set()
        for key in sorted(client.scan_iter(match=KEY_PREFIX + "*", count=100)):
            raw = client.get(key)
            if not raw:
                continue
            snap = json.loads(raw)
            job = snap.get("job", key[len(KEY_PREFIX):])
            lines.append(f"connect_job_last_run_timestamp_seconds{{job=\"{job}\"}} {snap.get('ts', 0)}")
            for s in snap.get("series", []):
                name = _metric_name(s.get("metric", ""))
                if name not in seen_types:
                    lines.append(f"# TYPE {name} gauge")
                    seen_types.add(name)
                points = s.get("points") or [[0, 0]]
                lines.append(f"{name}{_labels(s.get('tags', []), job)} {float(points[-1][1])}")
        return "\n".join(lines) + "\n"
    finally:
        client.close()
