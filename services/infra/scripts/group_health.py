#!/usr/bin/env python3
"""group_health.py — VM-group + per-sender health/reputation monitor (READ-ONLY).

ESP-style observability for shared mail pools. Two layers, both read-only:

  GROUP level (capacity/placement):
    per shared+active VM group of the provider — delivered volume, deferral rate,
    capacity headroom. Powers the claim placement view + near-capacity alerts.

  SENDER level (reputation / "move the bad ones"):
    per sending domain (rbm<...> subdomain) — deferral rate, HARD-bounce rate
    (dsn 5.x), and complaint count (from the suppressions table). Classifies each
    sender and recommends an action:
      - quarantine_candidate : hard-bounce% or complaint% over threshold → TOXIC.
        Isolate onto the (sacrificial, hard-throttled) quarantine pool so it stops
        contaminating the shared good IPs.
      - pace_candidate       : deferral% over threshold (ISP throttling/greylist) →
        slow the sender down; NOT toxic, do NOT quarantine.
      - healthy              : nothing to do.

It emits one JSON line per group (vm_group_health) and per flagged sender
(vm_sender_health), so a log pipeline can turn them into metrics and alerts. It NEVER
moves a zone: quarantining (an IP swap) stays a deliberate operator action.
Unsubscribes are list hygiene, NOT reputation — excluded.

It also audits relay coverage (relay_coverage / relay_volume events): every active
email_config whose smtp_host resolves to the submission host (SUBMISSION_HOST) and has NO
zone_relays binding leaves through the DEFAULT relay (relays.is_default=1, read from the
database, never assumed). If that default is Google's SMTP relay (smtp-relay.gmail.com), it
silently DROPS mail above ~10k/day (Postfix gets a 250 and the message never arrives), so
the cap/relay_cap_breach applies. Any other default relay has no such ceiling and the cap
is not emitted (see _default_relay).

Env:
  HEALTH_PROVIDER          provider to monitor (default: connect)
  HEALTH_WINDOW_DAYS       rolling window in days (default: 2)
  HEALTH_DAILY_CAP         safe delivered/day per group at full warmup (default: 50000)
  HEALTH_CAP_WARN          group load/capacity warn ratio 0-1 (default: 0.80)
  HEALTH_MIN_VOL           min attempts to classify a sender (default: 200)
  HEALTH_PACE_DEFER        deferral rate → pace_candidate (default: 0.10)
  HEALTH_QUAR_BOUNCE       hard-bounce rate → quarantine_candidate (default: 0.05)
  HEALTH_QUAR_COMPLAINT    complaint rate → quarantine_candidate (default: 0.001)
  SUBMISSION_HOST          shared submission hostname (default: smtp.example.com);
                           the IP compared is whatever it resolves to on each run
  SUBMISSION_IP_OVERRIDE   forces a fixed IP instead of resolving SUBMISSION_HOST (optional)
  RELAY_DEFAULT_DAILY_CAP  daily cap of the default relay when it is Google's (default: 10000)
  DB_*                     MySQL connection (same as mail_infra)
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys

os.environ.setdefault("OTEL_SDK_DISABLED", "true")

PROVIDER = os.getenv("HEALTH_PROVIDER", "connect")
WINDOW_DAYS = int(os.getenv("HEALTH_WINDOW_DAYS", "2"))
DAILY_CAP = int(os.getenv("HEALTH_DAILY_CAP", "50000"))
CAP_WARN = float(os.getenv("HEALTH_CAP_WARN", "0.80"))
MIN_VOL = int(os.getenv("HEALTH_MIN_VOL", "200"))
PACE_DEFER = float(os.getenv("HEALTH_PACE_DEFER", "0.10"))
QUAR_BOUNCE = float(os.getenv("HEALTH_QUAR_BOUNCE", "0.05"))
QUAR_COMPLAINT = float(os.getenv("HEALTH_QUAR_COMPLAINT", "0.001"))
# Pipeline health (outbox + send_ledger): thresholds of the alert events.
OUTBOX_PENDING_MAX = int(os.getenv("OUTBOX_PENDING_MAX", "20000"))
OUTBOX_OLDEST_SEC_MAX = int(os.getenv("OUTBOX_OLDEST_SEC_MAX", "900"))
LEDGER_STUCK_GRACE_SEC = int(os.getenv("LEDGER_STUCK_GRACE_SEC", "7200"))
# The installation's Postfix = whatever SUBMISSION_HOST resolves to, looked up on every run:
# comparing with a fixed IP would silently zero the coverage after the host's IP changes.
SUBMISSION_HOST = os.getenv("SUBMISSION_HOST", "smtp.example.com")
SUBMISSION_IP_OVERRIDE = os.getenv("SUBMISSION_IP_OVERRIDE") or None
RELAY_DEFAULT_DAILY_CAP = int(os.getenv("RELAY_DEFAULT_DAILY_CAP", "10000"))

LOG = logging.getLogger("group_health")
COLL = "COLLATE utf8mb4_0900_ai_ci"


def _rate(n, d):
    return round(n / d, 4) if d else 0.0


def _group_rows(cnx):
    """Per shared+active group of PROVIDER: delivered + deferral + free zones (window-scoped)."""
    cur = cnx.cursor(dictionary=True)
    cur.execute(
        f"""
        SELECT vg.group_key, vg.label, vg.mgmt_host,
               COALESCE(ld.delivered, 0) AS delivered,
               COALESCE(ld.deferred, 0)  AS deferred,
               COALESCE(ld.attempts, 0)  AS attempts,
               (SELECT COUNT(*) FROM zones z2
                  WHERE z2.group_id = vg.id AND z2.status = 'success_provision_infrastructure'
                    AND NOT EXISTS (SELECT 1 FROM customer_zones cz WHERE cz.zone_id = z2.id)) AS free_zones
        FROM vm_groups vg
        LEFT JOIN (
            SELECT z.group_id,
                   SUM(de.status = 'delivered') AS delivered,
                   SUM(de.status = 'deferred')  AS deferred,
                   COUNT(*) AS attempts
            FROM delivery_events de
            JOIN zones z ON z.subdomain_customer = SUBSTRING_INDEX(de.domain_name, '.', 1) {COLL}
            WHERE de.ts >= NOW() - INTERVAL %s DAY
            GROUP BY z.group_id
        ) ld ON ld.group_id = vg.id
        WHERE vg.provider = %s AND vg.kind = 'shared' AND vg.status = 'active'
        ORDER BY vg.group_key
        """,
        (WINDOW_DAYS, PROVIDER),
    )
    return list(cur.fetchall() or [])


def _sender_rows(cnx):
    """Per sending domain (shared+active group of PROVIDER): delivered/deferred/hard-bounce."""
    cur = cnx.cursor(dictionary=True)
    cur.execute(
        f"""
        SELECT de.domain_name, vg.group_key,
               SUM(de.status = 'delivered') AS delivered,
               SUM(de.status = 'deferred')  AS deferred,
               SUM(de.status = 'bounced' OR de.dsn LIKE '5.%%') AS hard_bounced,
               COUNT(*) AS attempts
        FROM delivery_events de
        JOIN zones z ON z.subdomain_customer = SUBSTRING_INDEX(de.domain_name, '.', 1) {COLL}
        JOIN vm_groups vg ON vg.id = z.group_id
        WHERE de.ts >= NOW() - INTERVAL %s DAY
          AND vg.provider = %s AND vg.kind = 'shared' AND vg.status = 'active'
        GROUP BY de.domain_name, vg.group_key
        """,
        (WINDOW_DAYS, PROVIDER),
    )
    return list(cur.fetchall() or [])


def _complaints(cnx):
    """domain_name -> active complaint count in window (real reputation signal; unsubscribes excluded)."""
    cur = cnx.cursor(dictionary=True)
    cur.execute(
        """
        SELECT domain_name, COUNT(*) AS complaints
        FROM suppressions
        WHERE suppression_type = 'complaint' AND suppressed_at >= NOW() - INTERVAL %s DAY
        GROUP BY domain_name
        """,
        (WINDOW_DAYS,),
    )
    return {r["domain_name"]: int(r["complaints"]) for r in (cur.fetchall() or [])}


def _relay_bindings(cnx):
    """Active email_configs + sending domain + zone_relays binding (NULL = default relay)."""
    cur = cnx.cursor(dictionary=True)
    cur.execute(
        """
        SELECT ec.smtp_host, vd.name AS sending_domain, r.name AS relay_name
        FROM email_configs ec
        JOIN virtual_domains vd ON vd.id = ec.domain_id
        LEFT JOIN zone_relays zr ON zr.zone_domain = vd.name
        LEFT JOIN relays r ON r.id = zr.relay_id
        WHERE ec.is_active = 1
        """
    )
    return list(cur.fetchall() or [])


def _volume_24h_by_sending_domain(cnx):
    """sending_domain -> delivered/deferred in the last 24 h (delivery_events uses
    domain_name = rbm<id>.<sending-domain>; the first label is dropped)."""
    cur = cnx.cursor(dictionary=True)
    cur.execute(
        """
        SELECT SUBSTRING(domain_name, LOCATE('.', domain_name) + 1) AS sending_domain,
               SUM(status = 'delivered') AS delivered,
               SUM(status = 'deferred')  AS deferred
        FROM delivery_events
        WHERE ts >= NOW() - INTERVAL 1 DAY
        GROUP BY SUBSTRING(domain_name, LOCATE('.', domain_name) + 1)
        """
    )
    return {r["sending_domain"]: r for r in (cur.fetchall() or [])}


def _default_relay(cnx):
    """The ACTUAL default relay (relays.is_default=1); never assume which one it is.

    The ~10k/day cap (RELAY_DEFAULT_DAILY_CAP) and the silent drop above it are properties
    of Google's SMTP relay (smtp-relay.gmail.com). Any other default relay has no such
    ceiling, and applying the cap to it would raise a false CRITICAL `relay_cap_breach` on
    every run and hide real signals.
    """
    cur = cnx.cursor(dictionary=True)
    cur.execute("SELECT name, smtp_host FROM relays WHERE is_default = 1 LIMIT 1")
    return cur.fetchone()


def _relay_coverage(cnx):
    """Emits relay_coverage + relay_volume: domains that submit through SUBMISSION_HOST
    without a zone_relays binding fall back to the DEFAULT relay (read from the database;
    the cap only applies when that default is Google's relay)."""
    bindings = _relay_bindings(cnx)
    volumes = _volume_24h_by_sending_domain(cnx)
    default_relay = _default_relay(cnx)
    default_label = "default-%s" % (default_relay["name"] if default_relay else "none")
    default_is_google = bool(
        default_relay and "gmail.com" in (default_relay.get("smtp_host") or "")
    )

    resolved: dict[str, str | None] = {}

    def _resolve(host: str) -> str | None:
        if host not in resolved:
            try:
                resolved[host] = socket.gethostbyname(host)
            except OSError:
                resolved[host] = None
        return resolved[host]

    submission_ip = SUBMISSION_IP_OVERRIDE or _resolve(SUBMISSION_HOST)
    if not submission_ip:
        # Without the reference IP the coverage would report 0 domains (a silent alert):
        # warn and stop.
        print(json.dumps({"event": "relay_coverage_error",
                          "reason": "submission host does not resolve",
                          "submission_host": SUBMISSION_HOST}), flush=True)
        return

    submission_domains, missing, by_relay = [], [], {}
    for b in bindings:
        if _resolve(b["smtp_host"]) != submission_ip:
            continue
        domain = b["sending_domain"]
        submission_domains.append(domain)
        relay = b["relay_name"] or default_label
        if b["relay_name"] is None:
            missing.append(domain)
        vol = volumes.get(domain, {})
        agg = by_relay.setdefault(relay, {"delivered_24h": 0, "deferred_24h": 0, "domains": 0})
        agg["delivered_24h"] += int(vol.get("delivered") or 0)
        agg["deferred_24h"] += int(vol.get("deferred") or 0)
        agg["domains"] += 1

    print(json.dumps({
        "event": "relay_coverage",
        "submission_domains": len(submission_domains),
        "missing_relay_count": len(missing),
        "missing_relay_domains": sorted(missing),
        # Which relay these domains ACTUALLY use: without it the alert would assume Google's
        # relay even when the default is another one (a false positive).
        "default_relay": default_relay["name"] if default_relay else None,
        "default_has_daily_cap": default_is_google,
    }), flush=True)

    # One event per domain without a relay: alerts can simply count events (no numeric
    # field has to be set up in advance).
    for domain in sorted(missing):
        print(json.dumps({"event": "relay_missing", "domain": domain}), flush=True)

    for relay, agg in sorted(by_relay.items()):
        # The cap only makes sense for Google's relay (~10k/day ceiling + silent drop).
        # Any other default relay has no such ceiling -> cap None -> no load_pct -> no
        # false relay_cap_breach.
        cap = RELAY_DEFAULT_DAILY_CAP if (relay == default_label and default_is_google) else None
        attempts = agg["delivered_24h"] + agg["deferred_24h"]
        load_pct = _rate(agg["delivered_24h"], cap) if cap else None
        print(json.dumps({
            "event": "relay_volume", "relay": relay, "domains": agg["domains"],
            "delivered_24h": agg["delivered_24h"], "deferred_24h": agg["deferred_24h"],
            "defer_rate": _rate(agg["deferred_24h"], attempts),
            "daily_cap": cap,
            "load_pct": load_pct,
        }), flush=True)
        # Above the cap the default relay answers 250 and DROPS the message: this event is
        # the only signal before the loss; 60% = warn, 80% = critical.
        if load_pct is not None and load_pct >= 0.6:
            print(json.dumps({
                "event": "relay_cap_breach", "relay": relay,
                "level": "critical" if load_pct >= 0.8 else "warn",
                "delivered_24h": agg["delivered_24h"], "daily_cap": cap,
                "load_pct": load_pct,
            }), flush=True)

    if missing:
        LOG.warning(
            "relay_coverage: %d submission-host domain(s) WITHOUT a zone_relays binding "
            "(they use the default route; a capped default relay drops mail above %d/day): %s",
            len(missing), RELAY_DEFAULT_DAILY_CAP, ", ".join(sorted(missing)),
        )



def _pipeline_health(cnx):
    """Emits the health of the event outbox (kafka_outbox table) and of send_ledger (alert events).

    outbox_health  : always (dashboards) — approximate total, exact pending count, age of
                     the oldest pending row (a stuck publisher = a growing age).
    outbox_alert   : pending > OUTBOX_PENDING_MAX or oldest > OUTBOX_OLDEST_SEC_MAX.
    ledger_stuck_alert: non-terminal rows stalled beyond the grace period (2 h). With the
                     reaper running this MUST be zero; >0 = the reaper is down/disabled or
                     a new kind of loss.
    """
    cur = cnx.cursor(dictionary=True)
    cur.execute(
        "SELECT TABLE_ROWS FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'kafka_outbox'"
    )
    row = cur.fetchone() or {}
    total_approx = int(row.get("TABLE_ROWS") or 0)
    cur.execute(
        "SELECT COUNT(*) AS pending, "
        "TIMESTAMPDIFF(SECOND, MIN(created_at), NOW()) AS oldest_sec "
        "FROM kafka_outbox WHERE status = 'pending'"
    )
    ob = cur.fetchone() or {}
    pending = int(ob.get("pending") or 0)
    oldest = int(ob.get("oldest_sec") or 0)
    print(json.dumps({
        "event": "outbox_health", "total_rows_approx": total_approx,
        "pending": pending, "oldest_pending_sec": oldest,
    }), flush=True)
    if pending > OUTBOX_PENDING_MAX or oldest > OUTBOX_OLDEST_SEC_MAX:
        print(json.dumps({
            "event": "outbox_alert", "pending": pending,
            "oldest_pending_sec": oldest,
            "pending_max": OUTBOX_PENDING_MAX, "oldest_sec_max": OUTBOX_OLDEST_SEC_MAX,
        }), flush=True)

    cur.execute(
        """
        SELECT
          SUM(state='smtp_accepted' AND delivery_deadline IS NOT NULL
                AND delivery_deadline < NOW() - INTERVAL %s SECOND) AS stuck_accepted,
          SUM(state='queued' AND admitted_at < NOW() - INTERVAL %s SECOND) AS stuck_queued,
          SUM(state='deferred' AND next_attempt_at IS NOT NULL
                AND next_attempt_at < NOW() - INTERVAL %s SECOND) AS stuck_deferred,
          SUM(state='sending' AND lease_expires_at IS NOT NULL
                AND lease_expires_at < NOW() - INTERVAL %s SECOND) AS stuck_sending
        FROM send_ledger
        WHERE state IN ('smtp_accepted','queued','deferred','sending')
        """,
        (LEDGER_STUCK_GRACE_SEC,) * 4,
    )
    st = cur.fetchone() or {}
    stuck = {k: int(st.get(k) or 0) for k in
             ("stuck_accepted", "stuck_queued", "stuck_deferred", "stuck_sending")}
    if any(stuck.values()):
        print(json.dumps({"event": "ledger_stuck_alert", **stuck,
                          "grace_sec": LEDGER_STUCK_GRACE_SEC}), flush=True)
        LOG.warning("ledger_stuck_alert: %s (grace=%ss)", stuck, LEDGER_STUCK_GRACE_SEC)


def main() -> int:
    # JSON on stdout. Plain-text logs on stderr would be read as errors by log collectors
    # and raise a false alert on every run.
    from connect_lib.logging import setup_logging
    setup_logging(service_name="mail-infra-group-health", json_format=True)
    from mail_infra.infrastructure.database import get_connection

    with get_connection() as cnx:
        groups = _group_rows(cnx)
        senders = _sender_rows(cnx)
        complaints = _complaints(cnx)
        try:
            _relay_coverage(cnx)
        except Exception:
            LOG.exception("relay coverage check failed (does not block group health)")
        try:
            _pipeline_health(cnx)
        except Exception:
            LOG.exception("pipeline health check failed (does not block group health)")

    # ---- group level (capacity / placement) ----
    LOG.info("group_health: %d shared %s group(s); window=%dd cap=%d", len(groups), PROVIDER, WINDOW_DAYS, DAILY_CAP)
    for g in groups:
        delivered = int(g["delivered"])
        attempts = int(g["attempts"]) or delivered
        per_day = delivered / max(1, WINDOW_DAYS)
        load_pct = _rate(per_day, DAILY_CAP)
        recs = []
        if load_pct > CAP_WARN:
            recs.append("near_capacity")
        if int(g["free_zones"] or 0) == 0:
            recs.append("pool_empty")
        print(json.dumps({
            "event": "vm_group_health", "group_key": g["group_key"], "label": g["label"],
            "mgmt_host": g["mgmt_host"], "window_days": WINDOW_DAYS,
            "delivered": delivered, "per_day": round(per_day),
            "defer_rate": _rate(int(g["deferred"]), attempts),
            "daily_cap": DAILY_CAP, "load_pct": load_pct,
            "free_zones": int(g["free_zones"] or 0), "recommendations": recs,
        }), flush=True)

    # ---- sender level (reputation / move-the-bad-ones) ----
    flagged = 0
    for s in senders:
        attempts = int(s["attempts"])
        if attempts < MIN_VOL:
            continue
        delivered = int(s["delivered"])
        defer_rate = _rate(int(s["deferred"]), attempts)
        hard_bounce_rate = _rate(int(s["hard_bounced"]), attempts)
        comp = complaints.get(s["domain_name"], 0)
        complaint_rate = _rate(comp, delivered or attempts)

        if hard_bounce_rate > QUAR_BOUNCE or complaint_rate > QUAR_COMPLAINT:
            action = "quarantine_candidate"
        elif defer_rate > PACE_DEFER:
            action = "pace_candidate"
        else:
            action = "healthy"

        if action == "healthy":
            continue
        flagged += 1
        print(json.dumps({
            "event": "vm_sender_health", "domain_name": s["domain_name"], "group_key": s["group_key"],
            "window_days": WINDOW_DAYS, "attempts": attempts, "delivered": delivered,
            "defer_rate": defer_rate, "hard_bounce_rate": hard_bounce_rate,
            "complaints": comp, "complaint_rate": complaint_rate, "action": action,
        }), flush=True)
        LOG.warning("sender %s [group %s]: %s (defer=%.1f%% hard_bounce=%.1f%% complaints=%d over %d attempts)",
                    s["domain_name"], s["group_key"], action, defer_rate * 100, hard_bounce_rate * 100, comp, attempts)

    LOG.info("group_health done; flagged_senders=%d", flagged)
    return 0


if __name__ == "__main__":
    sys.exit(main())
