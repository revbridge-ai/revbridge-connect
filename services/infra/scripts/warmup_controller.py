#!/usr/bin/env python3
"""Adaptive warm-up controller + budget gate per sending node.

Closed loop per node over delivery_events. Three states:

  warming  -> zones on the node itself. Measures health over the window and the 24 h volume.
              * green  -> keeps using the node; the budget FOLLOWS the volume the node has
                          already shown it delivers healthily (floor) + headroom.
              * yellow (blocks rising) + above the ceiling -> OVERFLOW of the EXCESS to
                          another route (protects the node without stopping the sender).
              * red (high block/bounce rate) -> BRAKE (all of the node's zones move away).
  overflow -> zones moved away because the node was stressed at its ceiling. They come back
              when the 24 h volume drops below the ceiling (hysteresis).
  braked   -> zones moved away because health turned red. They come back after N REAL
              green cycles (with deliveries), re-warming from the low step.

The node carries only what it can take (the warm-up curve); the excess and whatever turns
sour leave through another route right away. Acting = zone_relays (database only, no SSH);
each zone's "home" = egress_tier_plan.dedicated_relay_id.

WC_DRY_RUN=true (default) -> decides and logs, does NOT act.

RevBridge Connect (no provider): the "pools" are the own nodes (mail_infra.topology). Braking
or overflowing MOVES the node's zones to the healthy own node with the most spare capacity;
restoring gives the zones back to their home node (egress_tier_plan). With no node available
nothing moves: the excess stays in the queue (the mail API's volume governor defers the day's
excess). When an operator configures an overflow relay (CONNECT_OVERFLOW_RELAY), that relay is
the target instead. The decision (decide) is the same in both cases.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

DRY_RUN = os.getenv("WC_DRY_RUN", "true").lower() in ("1", "true", "yes", "on")
WINDOW_MIN = int(os.getenv("WC_WINDOW_MIN", "60"))
MIN_SENT = int(os.getenv("WC_MIN_SENT", "50"))
RED_BLOCK = float(os.getenv("WC_RED_BLOCK_PCT", "10"))
RED_BOUNCE = float(os.getenv("WC_RED_BOUNCE_PCT", "25"))
RED_MIN_EVENTS = int(os.getenv("WC_RED_MIN_EVENTS", "5"))
YELLOW_BLOCK = float(os.getenv("WC_YELLOW_BLOCK_PCT", "3"))
RAMP_FACTOR = float(os.getenv("WC_RAMP_FACTOR", "1.35"))
BUDGET_START = int(os.getenv("WC_BUDGET_START", "200"))
BUDGET_MAX = int(os.getenv("WC_BUDGET_MAX", "250000"))
GREEN_TO_RESTORE = int(os.getenv("WC_GREEN_TO_RESTORE", "3"))
# overflow hysteresis: go back to the node only when the 24 h volume drops below budget*margin
RESTORE_MARGIN = float(os.getenv("WC_OVERFLOW_RESTORE_MARGIN", "0.85"))
# canary: after N hours braked (with no traffic to prove its health) reopen a trickle at
# BUDGET_START to test the node. budget=0 = the operator parked it for good (no canary).
BRAKE_COOLDOWN_H = int(os.getenv("WC_BRAKE_COOLDOWN_H", "48"))


def emit(event: str, **kw) -> None:
    print(json.dumps({"event": event, **kw}, default=str), flush=True)


def decide(*, state: str, bounce_rate: float, block_rate: float,
           sent_window: int, sent_24h: int, budget: int, consecutive_green: int,
           block_events: int = 999, bounce_events: int = 999,
           brake_cooldown_over: bool = False) -> dict:
    """Pure decision (testable). Returns action + state + budget + consecutive_green.

    action: brake | overflow | restore | ramp | hold | insufficient
    The target of brake/overflow is another route; restore goes back to the home node.
    """
    red = ((block_rate >= RED_BLOCK and block_events >= RED_MIN_EVENTS)
           or (bounce_rate >= RED_BOUNCE and bounce_events >= RED_MIN_EVENTS))

    # RED always brakes (the traffic moves away), from any state. Resets the streak.
    if red:
        return {"action": "brake", "state": "braked",
                "budget": budget, "consecutive_green": 0}

    if state == "overflow":
        # moved away because the ceiling was reached. Comes back when the 24 h volume
        # drops (hysteresis).
        if sent_24h < budget * RESTORE_MARGIN:
            return {"action": "restore", "state": "warming",
                    "budget": budget, "consecutive_green": 0}
        return {"action": "hold", "state": "overflow",
                "budget": budget, "consecutive_green": 0}

    if state == "braked":
        # budget=0 = operator HARD-PARK: stays away for good and never comes back on its
        # own, neither by canary nor by traffic (e.g. draining a backlog through the IP does
        # not count as "ready for campaigns"). The operator releases it by setting budget>0.
        if budget <= 0:
            return {"action": "insufficient", "state": "braked",
                    "budget": budget, "consecutive_green": 0}
        # moved away because of bad health. Needs N REAL GREEN cycles to come back.
        if sent_window < MIN_SENT:  # no deliveries = no proof of health; does not count
            # canary: without traffic the node never proves it is green (its zones are
            # elsewhere). After the cooldown, reopen a trickle to test it, as long as it is
            # not parked (budget>0).
            if brake_cooldown_over and budget > 0:
                return {"action": "restore", "state": "warming",
                        "budget": BUDGET_START, "consecutive_green": 0}
            return {"action": "insufficient", "state": "braked",
                    "budget": budget, "consecutive_green": 0}
        cg = consecutive_green + 1
        if cg >= GREEN_TO_RESTORE:
            return {"action": "restore", "state": "warming",
                    "budget": BUDGET_START, "consecutive_green": 0}
        return {"action": "hold", "state": "braked",
                "budget": budget, "consecutive_green": cg}

    # state == warming (on the node itself)
    # small sample: health cannot be judged -> change nothing
    if sent_window < MIN_SENT:
        return {"action": "insufficient", "state": "warming",
                "budget": budget, "consecutive_green": consecutive_green}

    if block_rate >= YELLOW_BLOCK:
        # YELLOW (stressed): freeze the budget and, if demand went past the ceiling, move
        # the EXCESS away (protects the node without stopping the sender).
        if sent_24h >= budget:
            return {"action": "overflow", "state": "overflow",
                    "budget": budget, "consecutive_green": 0}
        return {"action": "hold", "state": "warming",
                "budget": budget, "consecutive_green": 0}

    # GREEN: the budget follows what the node has ALREADY shown it delivers healthily
    # (floor = 24 h volume) + headroom; a green node never overflows (it keeps using its own
    # IP while it is healthy). Growth is gated by health: if it grows too much and turns
    # sour, the block/bounce check fires and it BRAKES.
    target = max(budget, sent_24h, BUDGET_START)
    new_budget = min(target + max(int(target * (RAMP_FACTOR - 1.0)), 100), BUDGET_MAX)
    return {"action": "ramp", "state": "warming",
            "budget": new_budget, "consecutive_green": consecutive_green + 1}


def main() -> int:
    from mail_infra.infrastructure.database import get_connection
    from mail_infra.topology import list_nodes, node_host_like, overflow_relay_id, pick_target_node

    moved = 0
    with get_connection() as cnx:
        cur = cnx.cursor(dictionary=True)

        # the overflow relay, if one is configured; None in Connect -> moves between own nodes.
        overflow_id = overflow_relay_id(cur)

        pools = list_nodes(cur)
        if not pools:
            emit("no_pools")
            return 0

        for pool in pools:
            pid, pname = pool["id"], pool["name"]

            # health over the window (bounces/blocks) at the node's final hop
            cur.execute(
                """
                SELECT SUM(status IN ('sent','delivered')) AS sent,
                       SUM(status='bounced') AS bounced,
                       SUM(status='bounced' AND dsn LIKE '5.7.%') AS block57
                FROM delivery_events
                WHERE host LIKE %s
                  AND created_at >= NOW() - INTERVAL %s MINUTE
                """,
                (node_host_like(pname), WINDOW_MIN),
            )
            r = cur.fetchone()
            sent_w = int(r["sent"] or 0)
            bounced = int(r["bounced"] or 0)
            block57 = int(r["block57"] or 0)
            denom = sent_w + bounced
            bounce_rate = round(100.0 * bounced / denom, 2) if denom else 0.0
            block_rate = round(100.0 * block57 / max(sent_w + block57, 1), 2)

            # the node's daily (24 h) volume -> budget gate
            cur.execute(
                """
                SELECT SUM(status IN ('sent','delivered')) AS sent24
                FROM delivery_events
                WHERE host LIKE %s
                  AND created_at >= NOW() - INTERVAL 24 HOUR
                """,
                (node_host_like(pname),),
            )
            sent_24h = int((cur.fetchone() or {}).get("sent24") or 0)

            cur.execute(
                """SELECT state, daily_budget, consecutive_green,
                          (braked_at IS NOT NULL
                           AND braked_at <= NOW() - INTERVAL %s HOUR) AS cooldown_over
                   FROM warmup_state WHERE pool_relay_id=%s""",
                (BRAKE_COOLDOWN_H, pid),
            )
            st = cur.fetchone() or {"state": "warming", "daily_budget": BUDGET_START,
                                    "consecutive_green": 0, "cooldown_over": 0}

            d = decide(state=st["state"], bounce_rate=bounce_rate, block_rate=block_rate,
                       sent_window=sent_w, sent_24h=sent_24h, budget=int(st["daily_budget"]),
                       consecutive_green=int(st["consecutive_green"]),
                       block_events=block57, bounce_events=bounced,
                       brake_cooldown_over=bool(st.get("cooldown_over")))

            obs = {"pool": pname, "from": st["state"], "action": d["action"],
                   "to": d["state"], "sent_w": sent_w, "sent_24h": sent_24h,
                   "budget": d["budget"], "bounce_pct": bounce_rate, "block_pct": block_rate}
            emit("warmup_obs", **obs)

            cur.execute(
                """
                INSERT INTO warmup_state (pool_relay_id, pool_name, state, daily_budget,
                    consecutive_green, last_bounce_rate, last_block_rate, last_sent, last_decision)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE state=VALUES(state), daily_budget=VALUES(daily_budget),
                    consecutive_green=VALUES(consecutive_green), last_bounce_rate=VALUES(last_bounce_rate),
                    last_block_rate=VALUES(last_block_rate), last_sent=VALUES(last_sent),
                    last_decision=VALUES(last_decision)
                """,
                (pid, pname, d["state"], d["budget"], d["consecutive_green"],
                 bounce_rate, block_rate, sent_w,
                 ("dry_run:" if DRY_RUN else "") + d["action"]),
            )

            # acting: move the node's zones <-> the target (home = egress_tier_plan).
            # RevBridge Connect: the target is the healthy own node with the most spare
            # capacity (no provider).
            if d["action"] in ("brake", "overflow", "restore"):
                to_overflow = d["action"] in ("brake", "overflow")
                cur.execute(
                    "SELECT zone_domain FROM egress_tier_plan WHERE dedicated_relay_id=%s", (pid,))
                home_zones = [z["zone_domain"] for z in cur.fetchall()]
                target_name = "overflow"
                if overflow_id:
                    cur_relay, tgt_relay = (pid, overflow_id) if to_overflow else (overflow_id, pid)
                elif to_overflow:
                    tgt = pick_target_node(cur, exclude_ids={int(pid)})
                    cur_relay, tgt_relay = pid, (int(tgt["id"]) if tgt else None)
                    target_name = tgt["name"] if tgt else "queue (no node with spare capacity)"
                else:
                    cur_relay, tgt_relay, target_name = None, pid, pname
                # only touch the zones that are on the expected source relay (restore in
                # Connect: every home zone that is currently OFF the node)
                zones = []
                for z in home_zones:
                    cur.execute("SELECT relay_id FROM zone_relays WHERE zone_domain=%s", (z,))
                    b = cur.fetchone()
                    if cur_relay is None:
                        if b and int(b["relay_id"]) != int(pid):
                            zones.append(z)
                    elif b and int(b["relay_id"]) == cur_relay:
                        zones.append(z)
                if tgt_relay is None:
                    zones = []  # Connect with no node available: hold in the queue, move nothing
                detail = json.dumps({**obs, "zones": zones, "target": target_name})
                if DRY_RUN or not zones:
                    cur.execute("INSERT INTO warmup_log (pool_name, action, detail) VALUES (%s,%s,%s)",
                                (pname, ("dry_run:" if DRY_RUN else "noop:") + d["action"], detail))
                    emit("warmup_action", dry_run=DRY_RUN, noop=not zones, **obs, zones=zones)
                else:
                    for z in zones:
                        cur.execute("UPDATE zone_relays SET relay_id=%s WHERE zone_domain=%s",
                                    (tgt_relay, z))
                    if d["action"] == "brake":
                        cur.execute("UPDATE warmup_state SET braked_at=NOW(), last_moved_at=NOW() WHERE pool_relay_id=%s", (pid,))
                    else:
                        cur.execute("UPDATE warmup_state SET last_moved_at=NOW() WHERE pool_relay_id=%s", (pid,))
                    cur.execute("INSERT INTO warmup_log (pool_name, action, detail) VALUES (%s,%s,%s)",
                                (pname, d["action"], detail))
                    emit("warmup_action", dry_run=False, **obs, zones=zones)
                    moved += 1

        cnx.commit()

    emit("done", moved=moved, dry_run=DRY_RUN, pools=len(pools))
    return 0


if __name__ == "__main__":
    sys.exit(main())
