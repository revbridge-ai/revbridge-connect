"""Helpers for the node failover test (they run INSIDE the `api` container). Local testing only.

    python /tmp/node_test.py setup  <bootstrap.json>      # registers the simulated node and points the zone to it
    python /tmp/node_test.py send   <bootstrap.json> <subject>
    python /tmp/node_test.py where  <subject>             # prints 'node' | 'control' | 'none'
    python /tmp/node_test.py state                        # zone route + node state
"""

import json
import socket
import sys
import time

import httpx
from mail_shared.db.mysql_sync import exec_sql, fetch_all, fetch_one

NODE_NAME = "node-sim.example.com"  # = relays.name = the node's hostname
NODE_HOST = "node-sim"              # compose service (Mailpit playing the node)


def zone_of(boot: dict) -> str:
    return boot["domain"]


def setup(boot: dict) -> None:
    if not fetch_one("SELECT id FROM relays WHERE name=%s", (NODE_NAME,)):
        exec_sql("INSERT INTO relays (name, smtp_host, smtp_port, tls_level, is_default) "
                 "VALUES (%s, %s, 1025, 'none', 0)", (NODE_NAME, NODE_HOST))
    rid = fetch_one("SELECT id FROM relays WHERE name=%s", (NODE_NAME,))["id"]
    exec_sql("INSERT INTO zone_relays (zone_domain, relay_id) VALUES (%s, %s) "
             "ON DUPLICATE KEY UPDATE relay_id=VALUES(relay_id)", (zone_of(boot), rid))
    print(json.dumps({"node_relay_id": rid, "node_ip": socket.gethostbyname(NODE_HOST)}))


def send(boot: dict, subject: str) -> None:
    r = httpx.post("http://127.0.0.1:8001/send", headers={"Authorization": f"Bearer {boot['api_key']}"}, json={
        "personalizations": [{"to": [{"address": f"route{int(time.time())}@example.net"}]}],
        "from": {"name": "Example Shop", "address": f"contact@{boot['domain']}"},
        "subject": subject,
        "content": [{"type": "text/plain", "value": "routing test"}],
    }, timeout=30)
    print(r.status_code, r.text[:120])


def where(subject: str) -> None:
    for _ in range(30):
        for label, base in (("node", "http://node-sim:8025"), ("control", "http://mailpit:8025")):
            msgs = httpx.get(f"{base}/api/v1/messages", timeout=10).json().get("messages", [])
            if any(m.get("Subject") == subject for m in msgs):
                print(label)
                return
        time.sleep(2)
    print("none")


def cleanup(boot: dict) -> None:
    """Undoes the setup: the zone goes back to the control node and the simulated node is removed (stack as it was)."""
    rid = (fetch_one("SELECT id FROM relays WHERE name=%s", (NODE_NAME,)) or {}).get("id")
    exec_sql("DELETE FROM zone_relays WHERE zone_domain = %s", (zone_of(boot),))
    if rid:
        exec_sql("DELETE FROM connect_node_state WHERE relay_id = %s", (rid,))
        exec_sql("DELETE FROM recipient_relays WHERE relay_id = %s", (rid,))
        exec_sql("DELETE FROM relays WHERE id = %s", (rid,))
    print(json.dumps({"cleanup": True, "removed_relay": rid}))


def state() -> None:
    rows = fetch_all("SELECT zr.zone_domain, r.name AS relay FROM zone_relays zr JOIN relays r ON r.id=zr.relay_id")
    nodes = fetch_all("SELECT r.name, s.state, s.listed_on, s.clean_streak FROM connect_node_state s "
                      "JOIN relays r ON r.id=s.relay_id")
    print(json.dumps({"zones": rows, "nodes": nodes}, default=str))


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "setup":
        setup(json.load(open(sys.argv[2])))
    elif cmd == "send":
        send(json.load(open(sys.argv[2])), sys.argv[3])
    elif cmd == "where":
        where(sys.argv[2])
    elif cmd == "state":
        state()
    elif cmd == "cleanup":
        cleanup(json.load(open(sys.argv[2])))
