"""RevBridge Connect: message list and headers from MySQL.

GET /stats/messages and /stats/message/headers (stats_router) build their responses from
the tables the email plane itself writes:
  send_ledger       (admission/queue, tenant = customer_id)
  delivery_events   (Vector -> events updater: delivered/bounced/deferred/failed)
  engagement_events (tracking: open/click/unsubscribe)
  message_meta      (subject and sender stored by POST /send)
"""

from __future__ import annotations

import os
from typing import Any

from mail_shared.db.mysql_sync import fetch_all_async, fetch_one_async

_STATUS_SQL = """
    CASE
      WHEN d.last_delivery = 'delivered' THEN 'delivered'
      WHEN d.last_delivery IN ('bounced', 'hard_bounced', 'bounce') THEN 'bounced'
      WHEN d.last_delivery = 'deferred' THEN 'deferred'
      WHEN d.last_delivery IN ('failed', 'expired') THEN 'failed'
      WHEN d.state = 'delivered' THEN 'delivered'
      WHEN d.state IN ('failed', 'deadletter') THEN 'failed'
      WHEN d.state = 'smtp_accepted' THEN 'sent'
      ELSE 'queued'
    END
"""

# ?status= and ?event_type= filters
_STATUS_FILTER = {
    "success": ("delivered",),
    "error": ("bounced", "failed"),
    "pending": ("sent", "deferred", "queued"),
}
_EVENT_FILTER = {
    "delivered": "x.delivery_status = 'delivered'",
    "opened": "x.opens > 0",
    "clicked": "x.clicks > 0",
    "bounced": "x.delivery_status = 'bounced'",
    "failed": "x.delivery_status = 'failed'",
    "deferred": "x.delivery_status = 'deferred'",
}
_DELIVERY_EVENT_NAME = {
    "delivered": "delivered",
    "bounced": "bounced",
    "hard_bounced": "bounced",
    "bounce": "bounced",
    "deferred": "deferred",
    "failed": "failed",
    "expired": "failed",
    "sent": "sent",
}
_ENGAGEMENT_EVENT_NAME = {"open": "opened", "click": "clicked", "unsubscribe": "unsubscribed"}


def _base_query(client_id: str, start_date: str | None, end_date: str | None,
                search: str | None) -> tuple[str, list[Any]]:
    where = ["sl.customer_id = %s"]
    params: list[Any] = [client_id]
    if start_date:
        where.append("sl.created_at >= %s")
        params.append(f"{start_date} 00:00:00")
    if end_date:
        where.append("sl.created_at <= %s")
        params.append(f"{end_date} 23:59:59")
    if search:
        where.append("(sl.rcpt LIKE %s OR mm.subject LIKE %s)")
        like = f"%{search}%"
        params += [like, like]
    inner = f"""
        SELECT sl.id, sl.message_id, sl.rcpt AS recipient, sl.domain_name, sl.state,
               sl.created_at AS first_seen, sl.updated_at AS last_update,
               mm.subject, mm.sender_email,
               (SELECT de.status FROM delivery_events de
                 WHERE de.message_id = sl.message_id AND de.rcpt = sl.rcpt
                 ORDER BY de.id DESC LIMIT 1) AS last_delivery,
               (SELECT COUNT(*) FROM engagement_events ee
                 WHERE ee.message_id = sl.message_id AND ee.rcpt = sl.rcpt
                   AND ee.type = 'open' AND ee.is_bot = 0) AS opens,
               (SELECT COUNT(*) FROM engagement_events ee
                 WHERE ee.message_id = sl.message_id AND ee.rcpt = sl.rcpt
                   AND ee.type = 'click' AND ee.is_bot = 0) AS clicks
        FROM send_ledger sl
        LEFT JOIN message_meta mm ON mm.message_id = sl.message_id
        WHERE {' AND '.join(where)}
    """
    query = f"SELECT d.*, {_STATUS_SQL} AS delivery_status FROM ({inner}) d"
    return query, params


async def messages_from_mysql(current_user: dict, domain_name: str, page: int, limit: int,
                              start_date: str | None, end_date: str | None, status: str | None,
                              event_type: str | None, search: str | None) -> dict:
    client_id = current_user.get("client_id")
    base, params = _base_query(client_id, start_date, end_date, search)
    outer_where: list[str] = []
    outer_params: list[Any] = []
    if event_type and event_type in _EVENT_FILTER:
        outer_where.append(_EVENT_FILTER[event_type])
    elif status and status in _STATUS_FILTER:
        vals = _STATUS_FILTER[status]
        outer_where.append("x.delivery_status IN (" + ", ".join(["%s"] * len(vals)) + ")")
        outer_params += list(vals)
    where_sql = ("WHERE " + " AND ".join(outer_where)) if outer_where else ""

    totals_row = await fetch_one_async(
        f"""
        SELECT COUNT(*) AS total,
               SUM(x.delivery_status IN ('sent', 'delivered', 'bounced', 'deferred')) AS sent,
               SUM(x.delivery_status = 'delivered') AS delivered,
               SUM(x.opens > 0) AS opened,
               SUM(x.clicks > 0) AS clicked,
               SUM(x.delivery_status = 'bounced') AS bounced,
               SUM(x.delivery_status = 'failed') AS failed,
               SUM(x.delivery_status = 'deferred') AS deferred
        FROM ({base}) x {where_sql}
        """,
        tuple(params + outer_params),
    ) or {}
    total = int(totals_row.get("total") or 0)
    totals = {k: int(totals_row.get(k) or 0) for k in
              ("total", "sent", "delivered", "opened", "clicked", "bounced", "failed", "deferred")}
    total_pages = max(1, (total + limit - 1) // limit)
    pagination = {"page": page, "limit": limit, "total": total, "total_pages": total_pages,
                  "has_next": page < total_pages, "has_prev": page > 1}
    if total == 0:
        return {"status": "ok", "domain_name": domain_name, "pagination": pagination,
                "totals": totals, "messages": [], "source": "mysql"}

    rows = await fetch_all_async(
        f"SELECT x.* FROM ({base}) x {where_sql} ORDER BY x.id DESC LIMIT %s OFFSET %s",
        tuple(params + outer_params + [limit, (page - 1) * limit]),
    )
    ids = sorted({r["message_id"] for r in rows})
    ph = ", ".join(["%s"] * len(ids))
    deliveries = await fetch_all_async(
        f"SELECT message_id, rcpt, status, dsn, host, COALESCE(timestamp, ts) AS at "
        f"FROM delivery_events WHERE message_id IN ({ph}) ORDER BY id",
        tuple(ids),
    )
    engagements = await fetch_all_async(
        f"SELECT message_id, rcpt, type, url, ip_address, user_agent, open_source, open_status, "
        f"open_confidence_score, is_bot, bot_reason, COALESCE(ts, created_at) AS at "
        f"FROM engagement_events WHERE message_id IN ({ph}) ORDER BY id",
        tuple(ids),
    )
    timeline_index: dict[tuple[str, str], list[dict]] = {}
    for d in deliveries:
        meta = {k: d[k] for k in ("dsn", "host") if d.get(k)}
        timeline_index.setdefault((d["message_id"], d["rcpt"]), []).append(
            {"event_type": _DELIVERY_EVENT_NAME.get(d["status"], d["status"]), "timestamp": d["at"], "metadata": meta})
    for e in engagements:
        meta: dict[str, Any] = {}
        if e.get("ip_address"):
            meta["ip_address"] = e["ip_address"]
        if e.get("user_agent"):
            meta["user_agent"] = e["user_agent"]
        if e["type"] == "click" and e.get("url"):
            meta["click_url"] = e["url"]
        for f in ("open_source", "open_status", "open_confidence_score"):
            if e.get(f) is not None:
                meta[f] = e[f]
        if e.get("is_bot"):
            meta["is_bot"] = True
            meta["bot_reason"] = e.get("bot_reason")
        timeline_index.setdefault((e["message_id"], e["rcpt"]), []).append(
            {"event_type": _ENGAGEMENT_EVENT_NAME.get(e["type"], e["type"]), "timestamp": e["at"], "metadata": meta})

    messages = []
    for r in rows:
        key = (r["message_id"], r["recipient"])
        timeline = [{"event_type": "queued", "timestamp": r["first_seen"], "metadata": {}}]
        if r["state"] in ("smtp_accepted", "delivered") or timeline_index.get(key):
            timeline.append({"event_type": "sent", "timestamp": r["first_seen"], "metadata": {}})
        timeline += sorted(timeline_index.get(key, []), key=lambda t: str(t["timestamp"]))
        journey: list[str] = []
        for t in timeline:
            if t["event_type"] not in journey:
                journey.append(t["event_type"])
        messages.append({
            "message_id": r["message_id"],
            "recipient": r["recipient"],
            "domain_name": r["domain_name"],
            "subject": r.get("subject"),
            "sender_email": r.get("sender_email"),
            "delivery_status": r["delivery_status"],
            "first_seen": r["first_seen"],
            "last_update": r["last_update"],
            "journey": journey,
            "last_event": timeline[-1],
            "timeline": timeline,
        })
    return {"status": "ok", "domain_name": domain_name, "pagination": pagination,
            "totals": totals, "messages": messages, "source": "mysql"}


async def message_headers_from_mysql(
    current_user: dict, message_id: str, rcpt: str | None = None
) -> dict | None:
    """Headers that Connect stores (subject, sender, recipients, Message-ID).

    One send (message_id) can carry many recipients, each getting its own copy. With
    `rcpt`, `to` is that copy's recipient; without it, every recipient of the send.
    """
    row = await fetch_one_async(
        "SELECT mm.subject, mm.sender_email, mm.created_at FROM message_meta mm "
        "WHERE mm.message_id = %s AND mm.customer_id = %s",
        (message_id, current_user.get("client_id")),
    )
    if not row:
        return None
    sql = "SELECT rcpt FROM send_ledger WHERE message_id = %s AND customer_id = %s"
    params: tuple = (message_id, current_user.get("client_id"))
    if rcpt:
        sql += " AND rcpt = %s"
        params += (rcpt.strip().lower(),)
    rcpts = await fetch_all_async(sql, params)
    domain = os.getenv("MESSAGE_ID_DOMAIN", "")
    headers = {
        "subject": row.get("subject"),
        "from": row.get("sender_email"),
        "to": ", ".join(r["rcpt"] for r in rcpts),
        "message-id": f"<{message_id}@{domain}>" if domain else message_id,
        "date": str(row.get("created_at")),
    }
    return {
        "status": "ok",
        "message_id": message_id,
        "headers": headers,
        "subject": row.get("subject"),
        "sender_email": row.get("sender_email"),
        "raw": "\n".join(f"{k}: {v}" for k, v in headers.items() if v),
        "source": "mysql",
    }
