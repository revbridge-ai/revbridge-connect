#!/usr/bin/env python3
"""Per-IP warm-up controller for the sending nodes (a classic warm-up curve + a health brake).

Each IP warms up on its own: it receives a growing daily cap of real mail (the milter routes a
share of the traffic, by recipient hash, up to the cap; the rest keeps its normal route). On a
clean day the cap goes up; if the IP turns sour (high bounce/block rate) or gets listed
(DNSBL), it pauses. When the cap reaches the full value, the IP becomes 'warm' and joins the
regular rotation.

This does NOT route mail (the milter does). It only measures per-IP health in delivery_events
and moves the cap/state in ip_warmup. Data plane = milter + header_checks.

IW_DRY_RUN=true (default) decides and logs, does NOT write.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

DRY_RUN = os.getenv("IW_DRY_RUN", "true").lower() in ("1", "true", "yes", "on")
WINDOW_H = int(os.getenv("IW_WINDOW_H", "24"))
MIN_SENT = int(os.getenv("IW_MIN_SENT", "20"))
CAP_START = int(os.getenv("IW_CAP_START", "50"))
CAP_FULL = int(os.getenv("IW_CAP_FULL", "50000"))
RAMP_FACTOR = float(os.getenv("IW_RAMP_FACTOR", "1.3"))
RED_BLOCK = float(os.getenv("IW_RED_BLOCK_PCT", "5"))
RED_BOUNCE = float(os.getenv("IW_RED_BOUNCE_PCT", "15"))
RED_MIN_EVENTS = int(os.getenv("IW_RED_MIN_EVENTS", "5"))
RESUME_CLEAN = int(os.getenv("IW_RESUME_CLEAN", "3"))
RAMP_INTERVAL_H = float(os.getenv("IW_RAMP_INTERVAL_H", "24"))

# --- hysteresis ------------------------------------------------------------
# Without a dead band, an IP whose rate hovers right at the threshold flips between pause and
# resume every hour, and every resume would reset the cap and the step clock. Resuming
# therefore requires the rates to drop well below the pause thresholds.
RESUME_BLOCK = float(os.getenv("IW_RESUME_BLOCK_PCT", "3"))
RESUME_BOUNCE = float(os.getenv("IW_RESUME_BOUNCE_PCT", "8"))

# Minimum sample for the RATE to count as a signal. The number of blocks can stay nearly
# constant while the volume varies by an order of magnitude, so on a small sample the rate
# measures the DENOMINATOR, not the reputation. Below this floor there is no pause by rate
# (DNSBL still applies, and it is not gated).
MIN_RATE_SENT = int(os.getenv("IW_MIN_RATE_SENT", "300"))

# Discard local parts: these are not real sends (the alias exists to be rejected), so they
# pollute both the numerator and the denominator of the health rates. A single system that
# writes to its own domain's no-reply address every few minutes could otherwise dominate the
# block signal of the whole fleet and keep the warm-up paused.
DISCARD_LOCALPARTS = [
    s.strip().lower() for s in
    os.getenv("IW_DISCARD_LOCALPARTS", "naoresponda,no-reply,noreply,postmaster,abuse").split(",")
    if s.strip()
]


def emit(event: str, **kw) -> None:
    print(json.dumps({"event": event, **kw}, default=str), flush=True)


def ramp_decide(*, state: str, dnsbl_listed: bool, cap: int, sent: int,
                bounce_rate: float, block_rate: float, consecutive_clean: int,
                block_events: int = 999, bounce_events: int = 999,
                hours_since_ramp: float | None = None,
                ramp_interval_h: float | None = None) -> dict:
    """Pure per-IP decision (testable).

    action: start | ramp | hold | pause | resume | promote | keep
    target state in 'state'; new cap in 'cap'; streak in 'consecutive_clean'.

    hours_since_ramp/ramp_interval_h: cadence gate of the step. The job runs HOURLY, so
    without the gate the cap would grow 1.3x PER HOUR instead of the classic DAILY warm-up
    step. With the gate it only ramps if the last step (ramp/start) was >= ramp_interval_h
    ago (default 24 h, env IW_RAMP_INTERVAL_H). None in hours_since_ramp = no history -> it
    may ramp. The RED brake (pause) is NOT gated: it brakes immediately.
    """
    # The rate is only a signal with a large enough sample; below the floor it measures the
    # denominator (see MIN_RATE_SENT). DNSBL does not depend on volume and stays outside this
    # guard: a listing is a fact, not a statistic.
    rate_is_reliable = sent >= MIN_RATE_SENT
    red_by_rate = ((block_rate >= RED_BLOCK and block_events >= RED_MIN_EVENTS)
                    or (bounce_rate >= RED_BOUNCE and bounce_events >= RED_MIN_EVENTS))
    red = dnsbl_listed or (rate_is_reliable and red_by_rate)

    # RED (turned sour / listed) -> PAUSE from any state (low cap; the milter stops routing to
    # this IP and that traffic keeps its normal route). Resets the streak.
    # The cap is HALVED instead of going back to the start: resetting it would throw away
    # weeks of the curve on every oscillation.
    if red and state in ("warming", "warm"):
        return {"action": "pause", "state": "paused",
                "cap": max(CAP_START, int(cap) // 2), "consecutive_clean": 0}

    if state == "cold":
        return {"action": "start", "state": "warming",
                "cap": CAP_START, "consecutive_clean": 0}

    if state == "paused":
        if dnsbl_listed:  # still listed: do not resume
            return {"action": "hold", "state": "paused",
                    "cap": cap, "consecutive_clean": 0}
        # HYSTERESIS: resume only well below the pause threshold, and after RESUME_CLEAN clean
        # runs in a row. Resuming on the first tick below the pause threshold would flip
        # between pause and resume every hour.
        limpo = block_rate < RESUME_BLOCK and bounce_rate < RESUME_BOUNCE
        if not limpo:
            return {"action": "hold", "state": "paused",
                    "cap": cap, "consecutive_clean": 0}
        cc = consecutive_clean + 1
        if cc >= RESUME_CLEAN:
            # keep the cap reduced by the pause; do not go back to the start
            return {"action": "resume", "state": "warming",
                    "cap": max(CAP_START, int(cap)), "consecutive_clean": 0}
        return {"action": "hold", "state": "paused",
                "cap": cap, "consecutive_clean": cc}

    if state == "warm":
        return {"action": "keep", "state": "warm",
                "cap": CAP_FULL, "consecutive_clean": consecutive_clean}

    # state == warming
    if sent < MIN_SENT:  # not enough volume to judge: hold
        return {"action": "hold", "state": "warming",
                "cap": cap, "consecutive_clean": consecutive_clean}
    # cadence gate: 1 step per ramp_interval_h (the job runs hourly)
    _interval = RAMP_INTERVAL_H if ramp_interval_h is None else ramp_interval_h
    if hours_since_ramp is not None and hours_since_ramp < _interval:
        return {"action": "hold", "state": "warming",
                "cap": cap, "consecutive_clean": consecutive_clean + 1}
    # green: move the cap up the curve
    new_cap = min(int(max(cap, CAP_START) * RAMP_FACTOR), CAP_FULL)
    if new_cap >= CAP_FULL:
        return {"action": "promote", "state": "warm",
                "cap": CAP_FULL, "consecutive_clean": consecutive_clean + 1}
    return {"action": "ramp", "state": "warming",
            "cap": new_cap, "consecutive_clean": consecutive_clean + 1}


def dnsbl_listed(ip: str) -> bool:
    """Spamhaus ZEN through the local resolver. 127.255.x = an error (rate limit), not a listing."""
    import socket
    rev = ".".join(reversed(ip.split(".")))
    try:
        r = socket.gethostbyname(f"{rev}.zen.spamhaus.org")
        return r.startswith("127.") and not r.startswith("127.255.")
    except socket.gaierror:
        return False
    except Exception as e:
        emit("dnsbl_error", ip=ip, err=str(e)[:60])
        return False


def ip_health(cur, pool_name: str) -> dict:
    """Bounce % / 5.7.x block % / sent of the IP over the window.

    The REAL signal comes from the node that delivers (plain Postfix, NO milter): the node
    logs the destination MX's final verdict with host = the node's hostname (see
    IW_HOST_TEMPLATE). A 5.7.x here = a reputation block by the MX (e.g. Gmail's "very low
    reputation"), which is the pause trigger.
    """
    # Excludes the discard aliases: the alias exists TO be rejected, so every attempt counts
    # as a 5.7.x block and poisons both the numerator and the denominator. Without this
    # filter, a single noisy sender could keep the warm-up paused for good.
    _ph = ",".join(["%s"] * len(DISCARD_LOCALPARTS)) if DISCARD_LOCALPARTS else "''"
    cur.execute(
        f"""SELECT SUM(status IN ('sent','delivered')) sent,
                   SUM(status='bounced') bounced,
                   SUM(status='bounced' AND dsn LIKE '5.7.%%') block57
            FROM delivery_events
            WHERE host LIKE %s
              AND created_at >= NOW() - INTERVAL %s HOUR
              AND LOWER(SUBSTRING_INDEX(rcpt, '@', 1)) NOT IN ({_ph})""",
        # the node's hostname (IW_HOST_TEMPLATE='{name}', the same value the node writes to
        # delivery_events.host)
        (os.getenv("IW_HOST_TEMPLATE", "{name}").replace("{name}", pool_name),
         WINDOW_H, *DISCARD_LOCALPARTS))
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
    moved = 0
    with get_connection() as cnx:
        cur = cnx.cursor(dictionary=True)
        cur.execute("SELECT ip, pool_name, state, daily_cap, consecutive_clean FROM ip_warmup")
        rows = cur.fetchall()
        for w in rows:
            ip = w["ip"]
            listed = dnsbl_listed(ip)
            h = ip_health(cur, w["pool_name"])
            # cadence gate: hours since the last step in the log. Only 'ramp'/'start' count
            # as a step: counting 'resume' would restart the 24 h clock on every resume, and
            # an IP that pauses and resumes often would never move up the curve.
            cur.execute(
                """SELECT TIMESTAMPDIFF(MINUTE, MAX(created_at), NOW()) / 60.0 AS hrs
                   FROM ip_warmup_log WHERE ip=%s AND action IN ('ramp','start')""",
                (ip,))
            _r = cur.fetchone()
            hours_since = float(_r["hrs"]) if _r and _r.get("hrs") is not None else None
            d = ramp_decide(state=w["state"], dnsbl_listed=listed, cap=int(w["daily_cap"]),
                            sent=h["sent"], bounce_rate=h["bounce_rate"], block_rate=h["block_rate"],
                            consecutive_clean=int(w["consecutive_clean"]),
                            block_events=h["block57"], bounce_events=h["bounced"],
                            hours_since_ramp=hours_since)
            emit("iw_obs", ip=ip, pool=w["pool_name"], listed=listed, state_from=w["state"],
                 action=d["action"], state_to=d["state"], cap=d["cap"], sent=h["sent"],
                 bounce_pct=h["bounce_rate"], block_pct=h["block_rate"])
            if not DRY_RUN:
                cur.execute(
                    """UPDATE ip_warmup SET state=%s, daily_cap=%s, consecutive_clean=%s,
                           last_bounce_rate=%s, last_block_rate=%s, last_sent=%s, last_decision=%s
                       WHERE ip=%s""",
                    (d["state"], d["cap"], d["consecutive_clean"], h["bounce_rate"],
                     h["block_rate"], h["sent"], d["action"], ip))
                if d["action"] in ("ramp", "pause", "promote", "resume", "start"):
                    cur.execute("INSERT INTO ip_warmup_log (ip, action, detail) VALUES (%s,%s,%s)",
                                (ip, d["action"], json.dumps({"cap": d["cap"], **h})))
                    moved += 1
        if not DRY_RUN:
            cnx.commit()
    emit("done", moved=moved, dry_run=DRY_RUN, ips=len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
