#!/usr/bin/env python3
"""Volume governor PER sending DOMAIN (reputation ramp-up).

Email reputation lives in the sending_domain (+ IP), not in the campaign. This controller keeps
a DAILY QUOTA per domain in domain_volume_ramp, following the classic domain warm-up rules:
  - never more than RAMP_FACTOR (1.3x) per day, capped at 2x the actual volume;
  - it only goes up if the domain USED the quota (>= UTIL_PCT) and its health is green;
  - red health (a high bounce/5.7.x block rate) brakes IMMEDIATELY (the quota is halved);
  - inactivity decays the quota; >30 days idle goes back to the bottom of the ladder (cold).

This script measures and moves the quota; the API enforces it on POST /send
(mail_api.services.volume_governor, VOLUME_GOVERNOR_MODE). See docs/deliverability.md.

Consumption (admitted) comes from send_ledger (what /send accepted, per domain_name); health
comes from delivery_events, matching the domain + its rbm<id>.<zone> envelope subdomains.
VR_DRY_RUN=true (default) decides and logs, does NOT write.
"""

from __future__ import annotations

import datetime
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

DRY_RUN = os.getenv("VR_DRY_RUN", "true").lower() in ("1", "true", "yes", "on")
WINDOW_H = int(os.getenv("VR_WINDOW_H", "24"))
QUOTA_MIN = int(os.getenv("VR_QUOTA_MIN", "200"))
QUOTA_MAX = int(os.getenv("VR_QUOTA_MAX", "500000"))
RAMP_FACTOR = float(os.getenv("VR_RAMP_FACTOR", "1.3"))
MAX_STEP_FACTOR = float(os.getenv("VR_MAX_STEP_FACTOR", "2.0"))
UTIL_PCT = float(os.getenv("VR_RAMP_UTIL_PCT", "80"))
INIT_HEADROOM = float(os.getenv("VR_INIT_HEADROOM", "1.2"))
RED_BLOCK = float(os.getenv("VR_RED_BLOCK_PCT", "5"))
RED_BOUNCE = float(os.getenv("VR_RED_BOUNCE_PCT", "15"))
RED_MIN_EVENTS = int(os.getenv("VR_RED_MIN_EVENTS", "20"))
YELLOW_BLOCK = float(os.getenv("VR_YELLOW_BLOCK_PCT", "3"))
YELLOW_BOUNCE = float(os.getenv("VR_YELLOW_BOUNCE_PCT", "8"))
BRAKE_FACTOR = float(os.getenv("VR_BRAKE_FACTOR", "0.5"))
IDLE_DECAY_START_D = int(os.getenv("VR_IDLE_DECAY_START_D", "7"))
IDLE_DECAY_FACTOR = float(os.getenv("VR_IDLE_DECAY_FACTOR", "0.9"))
COLD_AFTER_D = int(os.getenv("VR_COLD_AFTER_D", "30"))
COLD_QUOTA = int(os.getenv("VR_COLD_QUOTA", "500"))
RAMP_INTERVAL_H = float(os.getenv("VR_RAMP_INTERVAL_H", "24"))
# LONG window, used only for the bootstrap. A 14-day window is blind to a sender that paused:
# a domain that sent a large daily volume, stopped for a few weeks and then came back would
# be created at the floor (200) and need weeks of ramp to return to its normal volume.
INIT_LOOKBACK_D = int(os.getenv("VR_INIT_LOOKBACK_D", "60"))

# actions that count as a "step" for the cadence gate (the job runs hourly; without the gate
# the quota would grow 1.3x PER HOUR, the same rule as in ip_warmup_controller)
STEP_ACTIONS = ("init", "ramp", "brake", "decay", "cool", "rewarm")


def emit(event: str, **kw) -> None:
    print(json.dumps({"event": event, **kw}, default=str), flush=True)


def init_quota(peak: int, idle_days: int = 0) -> int:
    """Starting quota of a domain: rebuilds the curve it WOULD have had.

    `peak` comes from the long window (INIT_LOOKBACK_D): a 14-day window does not see a
    sender that paused and would create a high-volume sender at the 200 floor. But giving
    the full historical peak to a domain idle for weeks is the opposite of a ramp-up, so the
    SAME inactivity decay the curve would have suffered is applied, as if the row had always
    existed (0.9/day from the 7th idle day on).

    A defensible middle ground: neither the absurd floor nor the full peak.
    """
    base = int(peak * INIT_HEADROOM)
    if idle_days > IDLE_DECAY_START_D:
        base = int(base * (IDLE_DECAY_FACTOR ** (idle_days - IDLE_DECAY_START_D)))
    return min(max(base, QUOTA_MIN), QUOTA_MAX)


def ramp_decide(*, state: str, quota: int, admitted_24h: int,
                bounce_rate: float, block_rate: float,
                bounce_events: int = 999, block_events: int = 999,
                idle_days: int = 0,
                hours_since_step: float | None = None,
                ramp_interval_h: float | None = None) -> dict:
    """Pure per-domain decision (testable).

    action: init | ramp | hold | brake | resume | decay | cool | rewarm
    Returns the target state in 'state' and the new quota in 'quota'.

    The RED brake (brake) does NOT respect the cadence gate: it brakes immediately.
    ramp/decay respect it (1 step per ramp_interval_h). hours_since_step=None means no step
    history -> it may move.
    """
    _interval = RAMP_INTERVAL_H if ramp_interval_h is None else ramp_interval_h
    gated = hours_since_step is not None and hours_since_step < _interval

    # the minimum mass applies to red AND yellow: 1 bounce in 10 sends = 10% of statistical
    # noise; without it a small domain would never leave hold
    red = ((block_rate >= RED_BLOCK and block_events >= RED_MIN_EVENTS)
           or (bounce_rate >= RED_BOUNCE and bounce_events >= RED_MIN_EVENTS))
    yellow = ((block_rate >= YELLOW_BLOCK and block_events >= RED_MIN_EVENTS)
              or (bounce_rate >= YELLOW_BOUNCE and bounce_events >= RED_MIN_EVENTS))

    if red and state != "braked":
        return {"action": "brake", "state": "braked",
                "quota": max(int(quota * BRAKE_FACTOR), QUOTA_MIN)}

    if state == "braked":
        if red:
            return {"action": "hold", "state": "braked", "quota": quota}
        # the window is clean again: resume with the braked quota; the ramp restarts from here
        return {"action": "resume", "state": "active", "quota": quota}

    if admitted_24h == 0:
        if idle_days > COLD_AFTER_D and state != "cold":
            return {"action": "cool", "state": "cold", "quota": COLD_QUOTA}
        if state == "cold":
            return {"action": "hold", "state": "cold", "quota": quota}
        if idle_days >= IDLE_DECAY_START_D and not gated:
            return {"action": "decay", "state": "active",
                    "quota": max(int(quota * IDLE_DECAY_FACTOR), QUOTA_MIN)}
        return {"action": "hold", "state": state, "quota": quota}

    if state == "cold":
        # sending again after going cold: the re-warm starts from the cold quota and ramps
        return {"action": "rewarm", "state": "active", "quota": max(quota, COLD_QUOTA)}

    if yellow or gated:
        return {"action": "hold", "state": "active", "quota": quota}

    if admitted_24h >= quota * UTIL_PCT / 100.0:
        new_quota = min(int(quota * RAMP_FACTOR),
                        int(admitted_24h * MAX_STEP_FACTOR), QUOTA_MAX)
        if new_quota > quota:
            return {"action": "ramp", "state": "active", "quota": new_quota}
    return {"action": "hold", "state": "active", "quota": quota}


def domain_health(cur, domain: str) -> dict:
    """Bounce % / 5.7.x block % of the domain over the window, from delivery_events.

    Matches the domain AND its subdomains (envelope rbm<id>.<zone>): without the LIKE the
    volume comes out as 0 and the health check is blind.

    Excludes recipients of the domain's OWN family (same zone, suffix in either direction):
    mail to `*@news.shop.example` sent with the key `rbm123456.news.shop.example` is OUR
    bounce/VERP traffic going back to the inbound MX, not a delivery to a real recipient,
    and its 5.7.1 rejections would brake the domain for no reason (a false positive).

    The window filters by `ts`, NOT by `created_at`: both columns hold the same insertion
    instant, but only `ts` is indexed (`idx_domain_ts (domain_name, ts)`). With `created_at`
    the filter becomes a full table scan PER DOMAIN, which on a large table makes the job
    exceed its timeout and freezes the curve.
    """
    cur.execute(
        """SELECT SUM(status IN ('sent','delivered')) sent,
                  SUM(status='bounced') bounced,
                  SUM(status='bounced' AND dsn LIKE '5.7.%%') block57
           FROM delivery_events
           WHERE (domain_name=%s OR domain_name LIKE CONCAT('%%.', %s))
             AND ts >= NOW() - INTERVAL %s HOUR
             AND NOT (SUBSTRING_INDEX(rcpt, '@', -1) = %s
                      OR SUBSTRING_INDEX(rcpt, '@', -1) LIKE CONCAT('%%.', %s)
                      OR %s LIKE CONCAT('%%.', SUBSTRING_INDEX(rcpt, '@', -1)))""",
        (domain, domain, WINDOW_H, domain, domain, domain))
    r = cur.fetchone()
    sent = int(r["sent"] or 0)
    bounced = int(r["bounced"] or 0)
    block57 = int(r["block57"] or 0)
    denom = sent + bounced
    return {
        "sent": sent, "bounced": bounced, "block57": block57,
        "bounce_rate": round(100.0 * bounced / denom, 2) if denom else 0.0,
        "block_rate": round(100.0 * block57 / max(sent + block57, 1), 2),
    }


def main() -> int:
    from mail_infra.infrastructure.database import get_connection
    today = datetime.date.today()
    moved = 0
    with get_connection() as cnx:
        cur = cnx.cursor(dictionary=True)

        cur.execute("SELECT domain_name, state, daily_quota, last_active_date, exempt "
                    "FROM domain_volume_ramp")
        governed = {r["domain_name"]: r for r in cur.fetchall()}

        # consumption admitted in the last 24 h per domain (what /send accepted)
        cur.execute(
            """SELECT domain_name, COUNT(*) c FROM send_ledger
               WHERE admitted_at >= NOW() - INTERVAL %s HOUR
               GROUP BY domain_name""", (WINDOW_H,))
        admitted = {r["domain_name"]: int(r["c"]) for r in cur.fetchall()}

        # daily peak + last day with sends over the LONG window. The bootstrap needs both:
        # the peak gives the domain's real capacity (a 14-day window misses a sender that
        # paused) and the last send says how long it has been idle, to rebuild the decay the
        # curve would have suffered.
        cur.execute(
            """SELECT domain_name, MAX(c) peak, DATEDIFF(CURDATE(), MAX(d)) idle_days
               FROM (
                 SELECT domain_name, DATE(admitted_at) d, COUNT(*) c
                 FROM send_ledger
                 WHERE admitted_at >= NOW() - INTERVAL %s DAY
                 GROUP BY domain_name, DATE(admitted_at)) t
               GROUP BY domain_name""", (INIT_LOOKBACK_D,))
        peaks = {
            r["domain_name"]: (int(r["peak"]), int(r["idle_days"] or 0))
            for r in cur.fetchall()
        }

        # bootstrap: a domain with traffic in the long window and no governor row yet
        for domain, (peak, idle_days) in peaks.items():
            if domain in governed:
                continue
            quota = init_quota(peak, idle_days)
            emit("vr_obs", domain=domain, action="init", state_to="active",
                 quota=quota, peak_long=peak, idle_days=idle_days,
                 admitted_24h=admitted.get(domain, 0))
            if not DRY_RUN:
                cur.execute(
                    """INSERT INTO domain_volume_ramp
                         (domain_name, state, daily_quota, baseline_peak, last_active_date)
                       VALUES (%s,'active',%s,%s,CURDATE())""",
                    (domain, quota, peak))
                cur.execute(
                    "INSERT INTO domain_volume_ramp_log (domain_name, action, detail) "
                    "VALUES (%s,'init',%s)",
                    (domain, json.dumps({
                        "quota": quota, "peak_long": peak,
                        "lookback_d": INIT_LOOKBACK_D, "idle_days": idle_days,
                    })))
                moved += 1

        for domain, row in governed.items():
            adm = admitted.get(domain, 0)
            h = domain_health(cur, domain)
            last_active = row["last_active_date"]
            idle_days = (today - last_active).days if last_active and adm == 0 else 0
            cur.execute(
                """SELECT TIMESTAMPDIFF(MINUTE, MAX(created_at), NOW()) / 60.0 AS hrs
                   FROM domain_volume_ramp_log
                   WHERE domain_name=%s AND action IN ({})""".format(
                    ",".join(["%s"] * len(STEP_ACTIONS))),
                (domain, *STEP_ACTIONS))
            _r = cur.fetchone()
            hours_since = float(_r["hrs"]) if _r and _r.get("hrs") is not None else None

            d = ramp_decide(state=row["state"], quota=int(row["daily_quota"]),
                            admitted_24h=adm, bounce_rate=h["bounce_rate"],
                            block_rate=h["block_rate"], bounce_events=h["bounced"],
                            block_events=h["block57"], idle_days=idle_days,
                            hours_since_step=hours_since)
            # exempt: the curve and the health are still observed, but the API's gate does
            # not hold the domain; would_defer is always 0 for exempt domains so they do not
            # pollute the decision metric
            exempt = bool(row.get("exempt"))
            emit("vr_obs", domain=domain, action=d["action"], state_from=row["state"],
                 state_to=d["state"], quota=d["quota"], admitted_24h=adm,
                 would_defer=0 if exempt else max(0, adm - int(row["daily_quota"])),
                 exempt=exempt, idle_days=idle_days, bounce_pct=h["bounce_rate"],
                 block_pct=h["block_rate"], sent=h["sent"])
            if not DRY_RUN:
                cur.execute(
                    """UPDATE domain_volume_ramp
                       SET state=%s, daily_quota=%s, last_admitted_24h=%s,
                           last_bounce_rate=%s, last_block_rate=%s, last_decision=%s,
                           last_active_date=IF(%s > 0, CURDATE(), last_active_date)
                       WHERE domain_name=%s""",
                    (d["state"], d["quota"], adm, h["bounce_rate"], h["block_rate"],
                     d["action"], adm, domain))
                if d["action"] in STEP_ACTIONS or d["action"] == "resume":
                    cur.execute(
                        "INSERT INTO domain_volume_ramp_log (domain_name, action, detail) "
                        "VALUES (%s,%s,%s)",
                        (domain, d["action"], json.dumps({"quota": d["quota"],
                                                          "admitted_24h": adm, **h})))
                    moved += 1
        if not DRY_RUN:
            cnx.commit()
    emit("done", moved=moved, dry_run=DRY_RUN,
         domains=len(governed), bootstrapped=len(set(peaks) - set(governed)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
