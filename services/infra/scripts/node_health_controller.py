#!/usr/bin/env python3
"""Node health controller (RevBridge Connect): a node whose IP is on a blocklist leaves the
rotation on its own.

Designed for the Connect topology (one control VM plus nodes reached over SSH, no third-party
provider). Like the other controllers it only acts on the database (Postfix reads it live):

  1. For each node (relays LIKE CONNECT_NODE_RELAY_LIKE; the control node is the 'direct'
     relay), finds the egress IP and queries the DNSBLs in NH_DNSBL_ZONES.
  2. Listed -> LISTED state and quarantine:
       - zone_relays pointing to the node    -> the healthy node with the most spare capacity
       - active recipient_relays on the node -> same
       - if the node is the default relay    -> the default moves to that node
     With no healthy node available: records 'no_target' and moves nothing (the queue holds
     the mail and Postfix retries; there is never an overflow to a provider).
  3. Clean for NH_CLEAN_TO_RELEASE checks in a row -> HEALTHY, and the zones THIS job moved
     away from the node go back to it (if they are still where the job put them).

127.255.255.x answers (the resolver was refused by Spamhaus) count as "no answer", never as a
listing. For Spamhaus to answer, use a recursive resolver of your own.

Env: NH_DRY_RUN (default true), NH_DNSBL_ZONES, NH_CLEAN_TO_RELEASE (default 3),
     CONNECT_CONTROL_IP (the control node's IP), NH_FORCE_LISTED (tests only: IPs treated as listed).
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

DRY_RUN = os.getenv("NH_DRY_RUN", "true").lower() in ("1", "true", "yes", "on")
DNSBL_ZONES = [z.strip() for z in os.getenv(
    "NH_DNSBL_ZONES", "zen.spamhaus.org,b.barracudacentral.org,bl.spamcop.net,psbl.surriel.com"
).split(",") if z.strip()]
CLEAN_TO_RELEASE = int(os.getenv("NH_CLEAN_TO_RELEASE", "3"))
CONTROL_IP = os.getenv("CONNECT_CONTROL_IP", "").strip()
FORCE_LISTED = {ip.strip() for ip in os.getenv("NH_FORCE_LISTED", "").split(",") if ip.strip()}


def emit(event: str, **kw) -> None:
    print(json.dumps({"event": event, **kw}, default=str), flush=True)


def node_ip(node: dict) -> str | None:
    host = (node.get("smtp_host") or "").strip()
    if host == "direct":
        return CONTROL_IP or None
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        try:
            return socket.gethostbyname(host)
        except OSError:
            return None


def dnsbl_listings(ip: str) -> tuple[list[str], list[str]]:
    """(listed_on, no_answer). Only public IPv4 addresses are queried."""
    if ip in FORCE_LISTED:
        return ["test:NH_FORCE_LISTED"], []
    addr = ipaddress.ip_address(ip)
    if addr.version != 4 or not addr.is_global:
        return [], []
    rev = ".".join(reversed(ip.split(".")))
    listed, unknown = [], []
    for zone in DNSBL_ZONES:
        try:
            ans = socket.gethostbyname(f"{rev}.{zone}")
        except socket.gaierror:
            continue  # NXDOMAIN = not listed
        except OSError:
            unknown.append(zone)
            continue
        if ans.startswith("127.255.255.") or ans.startswith("127.0.0.255"):
            unknown.append(zone)  # query refused (public resolver) -> not a listing
        elif ans.startswith("127."):
            listed.append(f"{zone}={ans}")
    return listed, unknown


def decide(state: str, listed: bool, clean_streak: int) -> tuple[str, int, str]:
    """Pure decision (testable): (new_state, new_streak, action)."""
    if state == "DISABLED":
        return "DISABLED", clean_streak, "hold"
    if listed:
        return "LISTED", 0, ("quarantine" if state != "LISTED" else "hold")
    if state == "LISTED":
        streak = clean_streak + 1
        if streak >= CLEAN_TO_RELEASE:
            return "HEALTHY", streak, "release"
        return "LISTED", streak, "hold"
    return "HEALTHY", clean_streak + 1, "ok"


def log(cur, relay_id: int, event: str, detail: dict) -> None:
    cur.execute(
        "INSERT INTO connect_node_log (relay_id, event, detail, dry_run) VALUES (%s, %s, %s, %s)",
        (relay_id, event, json.dumps(detail, default=str), 1 if DRY_RUN else 0),
    )


def quarantine(cur, node: dict) -> int:
    from mail_infra.topology import pick_target_node

    nid = int(node["id"])
    target = pick_target_node(cur, exclude_ids={nid})
    if not target:
        log(cur, nid, "no_target", {"node": node["name"]})
        emit("no_target", node=node["name"], dry_run=DRY_RUN)
        return 0
    tid = int(target["id"])
    moved = 0
    cur.execute("SELECT zone_domain FROM zone_relays WHERE relay_id=%s", (nid,))
    for z in [r["zone_domain"] for r in cur.fetchall()]:
        log(cur, nid, "zone_moved", {"zone": z, "from": nid, "to": tid})
        emit("zone_moved", zone=z, frm=node["name"], to=target["name"], dry_run=DRY_RUN)
        if not DRY_RUN:
            cur.execute("UPDATE zone_relays SET relay_id=%s WHERE zone_domain=%s", (tid, z))
        moved += 1
    cur.execute("SELECT recipient_domain FROM recipient_relays WHERE relay_id=%s AND is_active=1", (nid,))
    for rd in [r["recipient_domain"] for r in cur.fetchall()]:
        log(cur, nid, "recipient_moved", {"recipient_domain": rd, "from": nid, "to": tid})
        if not DRY_RUN:
            cur.execute("UPDATE recipient_relays SET relay_id=%s WHERE recipient_domain=%s", (tid, rd))
        moved += 1
    if int(node.get("is_default") or 0) == 1 or (node.get("smtp_host") == "direct" and not _has_default(cur)):
        # traffic without an explicit route was leaving through this node: the target becomes the default
        log(cur, nid, "default_moved", {"from": nid, "to": tid})
        emit("default_moved", frm=node["name"], to=target["name"], dry_run=DRY_RUN)
        if not DRY_RUN:
            cur.execute("UPDATE relays SET is_default=0 WHERE is_default=1")
            cur.execute("UPDATE relays SET is_default=1 WHERE id=%s", (tid,))
        moved += 1
    return moved


def _has_default(cur) -> bool:
    cur.execute("SELECT COUNT(*) AS c FROM relays WHERE is_default=1")
    return int((cur.fetchone() or {}).get("c") or 0) > 0


def release(cur, node: dict, changed_at) -> int:
    """Gives back to the node what THIS job moved away from it since the quarantine (if it is still on the target)."""
    nid = int(node["id"])
    cur.execute(
        "SELECT event, detail FROM connect_node_log WHERE relay_id=%s AND dry_run=0 "
        "AND event IN ('zone_moved','recipient_moved','default_moved') AND created_at >= %s ORDER BY id",
        (nid, changed_at),
    )
    back = 0
    for r in cur.fetchall():
        d = json.loads(r["detail"] or "{}")
        if r["event"] == "zone_moved":
            cur.execute("UPDATE zone_relays SET relay_id=%s WHERE zone_domain=%s AND relay_id=%s",
                        (nid, d.get("zone"), d.get("to")))
        elif r["event"] == "recipient_moved":
            cur.execute("UPDATE recipient_relays SET relay_id=%s WHERE recipient_domain=%s AND relay_id=%s",
                        (nid, d.get("recipient_domain"), d.get("to")))
        elif r["event"] == "default_moved":
            cur.execute("UPDATE relays SET is_default=0 WHERE id=%s", (d.get("to"),))
            if node.get("smtp_host") != "direct":
                cur.execute("UPDATE relays SET is_default=1 WHERE id=%s", (nid,))
        back += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return back


def main() -> int:
    from mail_infra.infrastructure.database import get_connection
    from mail_infra.topology import list_nodes

    gauges = []
    with get_connection() as cnx:
        cur = cnx.cursor(dictionary=True)
        nodes = list_nodes(cur)
        if not nodes:
            emit("no_nodes")
            return 0
        for node in nodes:
            nid = int(node["id"])
            ip = node_ip(node)
            listed, unknown = dnsbl_listings(ip) if ip else ([], [])
            cur.execute("SELECT state, clean_streak, changed_at FROM connect_node_state WHERE relay_id=%s", (nid,))
            st = cur.fetchone() or {"state": "HEALTHY", "clean_streak": 0, "changed_at": None}
            new_state, streak, action = decide(st["state"], bool(listed), int(st["clean_streak"] or 0))
            moved = 0
            if action == "quarantine":
                log(cur, nid, "listed", {"node": node["name"], "ip": ip, "listed_on": listed})
                moved = quarantine(cur, node)
            elif action == "release" and not DRY_RUN:
                moved = release(cur, node, st.get("changed_at"))
                log(cur, nid, "released", {"node": node["name"], "ip": ip, "returned": moved})
            changed = new_state != st["state"]
            if not DRY_RUN or not changed:
                cur.execute(
                    """
                    INSERT INTO connect_node_state (relay_id, ip, state, listed_on, clean_streak, zones_moved,
                                                    last_check_at, changed_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
                    ON DUPLICATE KEY UPDATE ip=VALUES(ip), state=VALUES(state), listed_on=VALUES(listed_on),
                        clean_streak=VALUES(clean_streak),
                        zones_moved=IF(%s, VALUES(zones_moved), zones_moved),
                        last_check_at=NOW(), changed_at=IF(%s, NOW(), changed_at)
                    """,
                    (nid, ip, new_state, ",".join(listed)[:512] or None, streak, moved,
                     1 if action == "quarantine" else 0, 1 if changed else 0),
                )
            emit("node_check", node=node["name"], ip=ip, state=new_state, action=action,
                 listed_on=listed, no_answer=unknown, moved=moved, dry_run=DRY_RUN)
            gauges.append({"metric": "node.listed", "points": [[0, 1.0 if listed else 0.0]],
                           "type": "gauge", "tags": [f"node:{node['name']}", f"ip:{ip}"]})
            gauges.append({"metric": "node.healthy", "points": [[0, 1.0 if new_state == "HEALTHY" else 0.0]],
                           "type": "gauge", "tags": [f"node:{node['name']}", f"ip:{ip}"]})
        cnx.commit()

    from mail_infra.metrics_sink import publish, sink_is_redis

    if sink_is_redis():
        publish("node_health", gauges, ttl_sec=3600)
    emit("done", nodes=len(nodes), dry_run=DRY_RUN)
    return 0


if __name__ == "__main__":
    sys.exit(main())
