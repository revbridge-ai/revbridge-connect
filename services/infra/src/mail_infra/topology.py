"""Egress topology: which nodes send mail and how to find them in the delivery logs.

The deliverability jobs need to know which `relays` rows are sending nodes and how each node
appears in delivery_events.host. This module keeps those naming rules in environment
variables; the defaults match a standard RevBridge Connect installation (no third-party
provider).

Connect model:
  - Each sending node is a row in `relays` with `name` = the node's hostname (the same
    hostname the node writes to delivery_events.host) and `smtp_host` = the relay's IP/host.
  - The control node is also a row in `relays`, with smtp_host = 'direct': routing to it
    means direct delivery from the control node's IP (Postfix treats 'direct' as "no relayhost").
  - There is no provider relay: CONNECT_OVERFLOW_RELAY is empty. When no healthy node has
    spare capacity, the message stays in the queue (the volume governor and Postfix hold it
    and retry).
"""

from __future__ import annotations

import os

DIRECT = "direct"

# name pattern (LIKE) of the `relays` rows that are sending nodes (in Connect, all of them)
NODE_RELAY_LIKE = os.getenv("CONNECT_NODE_RELAY_LIKE", "%")
# delivery_events host pattern for a node; {name} = relays.name (= the node's hostname)
NODE_HOST_TEMPLATE = os.getenv("CONNECT_NODE_HOST_TEMPLATE", "{name}")
# delivery_events host pattern of the control node (its hostname)
CONTROL_HOST_LIKE = os.getenv("CONNECT_CONTROL_HOST_LIKE", os.getenv("MYHOSTNAME", "%"))
# overflow relay: empty = none (excess mail waits in the queue).
# Only for operators who want to add an external backup relay of their own.
OVERFLOW_RELAY = os.getenv("CONNECT_OVERFLOW_RELAY", "").strip()


def node_host_like(relay_name: str) -> str:
    """LIKE pattern of the delivery_events host that delivers through this node (pass it as a query parameter)."""
    return NODE_HOST_TEMPLATE.replace("{name}", relay_name)


def overflow_relay_id(cur) -> int | None:
    """id of the overflow relay, if one is configured and exists. None in a default installation."""
    if not OVERFLOW_RELAY:
        return None
    cur.execute("SELECT id FROM relays WHERE name = %s LIMIT 1", (OVERFLOW_RELAY,))
    row = cur.fetchone()
    if not row:
        return None
    return int(row["id"] if isinstance(row, dict) else row[0])


def list_nodes(cur) -> list[dict]:
    """Sending nodes (includes the control node's 'direct' row, if registered)."""
    cur.execute(
        "SELECT id, name, smtp_host, smtp_port, is_default FROM relays WHERE name LIKE %s ORDER BY id",
        (NODE_RELAY_LIKE,),
    )
    rows = cur.fetchall()
    out = []
    for r in rows:
        d = r if isinstance(r, dict) else dict(zip(("id", "name", "smtp_host", "smtp_port", "is_default"), r))
        if OVERFLOW_RELAY and d["name"] == OVERFLOW_RELAY:
            continue
        out.append(d)
    return out


def node_health_states(cur) -> dict[int, str]:
    """relay_id -> node_health_controller state (HEALTHY when there is no row)."""
    try:
        cur.execute("SELECT relay_id, state FROM connect_node_state")
    except Exception:  # table missing: treat every node as healthy
        return {}
    states = {}
    for r in cur.fetchall():
        d = r if isinstance(r, dict) else {"relay_id": r[0], "state": r[1]}
        states[int(d["relay_id"])] = d["state"]
    return states


def sent_24h_by_node(cur, nodes: list[dict]) -> dict[int, int]:
    """Deliveries in the last 24 h per node (by the final hop host in delivery_events)."""
    out: dict[int, int] = {}
    for n in nodes:
        cur.execute(
            "SELECT COUNT(*) AS c FROM delivery_events WHERE host LIKE %s "
            "AND status IN ('sent','delivered') AND ts >= NOW() - INTERVAL 24 HOUR",
            (node_host_like(n["name"]),),
        )
        r = cur.fetchone() or {}
        out[int(n["id"])] = int((r.get("c") if isinstance(r, dict) else r[0]) or 0)
    return out


def pick_target_node(cur, *, exclude_ids: set[int], prefer_low_volume: bool = True) -> dict | None:
    """Healthy node to take over traffic moved away from another node.

    Criteria: HEALTHY state in node_health_controller, not in `exclude_ids`, and the lowest
    volume in the last 24 h (most spare capacity). None = no node available (the traffic waits
    in the queue).
    """
    nodes = [n for n in list_nodes(cur) if int(n["id"]) not in exclude_ids]
    if not nodes:
        return None
    states = node_health_states(cur)
    healthy = [n for n in nodes if states.get(int(n["id"]), "HEALTHY") == "HEALTHY"]
    if not healthy:
        return None
    if not prefer_low_volume:
        return healthy[0]
    vol = sent_24h_by_node(cur, healthy)
    return min(healthy, key=lambda n: (vol.get(int(n["id"]), 0), int(n["id"])))
