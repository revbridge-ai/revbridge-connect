"""Load test of the local stack: how many emails per second Connect accepts and delivers.

Runs INSIDE the `api` container against the dev stack (the MTA delivers to Mailpit, nothing
leaves the machine):

    docker compose exec -T api python /tmp/loadtest.py /tmp/bootstrap.json \
        --messages 20000 --batch 50 --concurrency 16

What it measures, from the send ledger (send_ledger: queued -> sending -> smtp_accepted ->
delivered):
  accept     POST /send admitted the message (API + queue write)
  submitted  the queue processor handed it to the MTA (milter + DKIM ran, Postfix said 250)
  delivered  Postfix reported status=sent and the event pipeline (Vector -> events) recorded it

Only for local testing with synthetic recipients (@loadtest.invalid style addresses on a
domain that the dev MTA routes to Mailpit).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

import httpx
from mail_shared.db.mysql_sync import exec_sql, fetch_all

API = "http://127.0.0.1:8001"


def ledger_counts(domain: str, since: float) -> dict[str, int]:
    rows = fetch_all(
        "SELECT state, COUNT(*) AS n FROM send_ledger "
        "WHERE domain_name = %s AND admitted_at >= FROM_UNIXTIME(%s) GROUP BY state",
        (domain, int(since) - 1),
    )
    return {r["state"]: int(r["n"]) for r in rows or []}


async def send_all(boot: dict, total: int, batch: int, concurrency: int, tag: str) -> dict:
    headers = {"Authorization": f"Bearer {boot['api_key']}"}
    domain = boot["domain"]
    sem = asyncio.Semaphore(concurrency)
    stats = {"requests": 0, "accepted": 0, "errors": 0, "status": {}}
    latencies: list[float] = []

    async def one(client: httpx.AsyncClient, start: int) -> None:
        n = min(batch, total - start)
        payload = {
            "personalizations": [
                {"to": [{"address": f"lt-{tag}-{start + i}@loadtest.example.org"}]} for i in range(n)
            ],
            "from": {"name": "Load Test", "address": f"news@{domain}"},
            "subject": f"Load test {tag}",
            "content": [
                {"type": "text/plain", "value": "Load test message. https://example.org/offer"},
                {"type": "text/html", "value": (
                    "<html><body><p>Load test message.</p>"
                    "<p><a href=\"https://example.org/offer?utm_source=loadtest\">Offer</a></p>"
                    "</body></html>"
                )},
            ],
        }
        async with sem:
            t = time.monotonic()
            try:
                r = await client.post(f"{API}/send", json=payload, headers=headers, timeout=60)
                latencies.append(time.monotonic() - t)
                stats["requests"] += 1
                stats["status"][str(r.status_code)] = stats["status"].get(str(r.status_code), 0) + 1
                if r.status_code < 300:
                    stats["accepted"] += n
                else:
                    stats["errors"] += 1
                    if stats["errors"] <= 3:
                        print(f"  POST /send {r.status_code}: {r.text[:200]}", flush=True)
            except Exception as e:  # noqa: BLE001
                stats["errors"] += 1
                if stats["errors"] <= 3:
                    print(f"  POST /send error: {type(e).__name__}: {e}", flush=True)

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(limits=limits) as client:
        t0 = time.monotonic()
        await asyncio.gather(*(one(client, s) for s in range(0, total, batch)))
        stats["seconds"] = round(time.monotonic() - t0, 2)
    latencies.sort()
    if latencies:
        stats["p50_ms"] = round(latencies[len(latencies) // 2] * 1000)
        stats["p95_ms"] = round(latencies[int(len(latencies) * 0.95) - 1] * 1000)
    stats["accept_per_s"] = round(stats["accepted"] / stats["seconds"], 1) if stats["seconds"] else 0
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bootstrap")
    ap.add_argument("--messages", type=int, default=5000)
    ap.add_argument("--batch", type=int, default=50, help="recipients per POST /send")
    ap.add_argument("--concurrency", type=int, default=16, help="parallel HTTP requests")
    ap.add_argument("--timeout", type=int, default=900, help="seconds to wait for delivery")
    args = ap.parse_args()
    boot = json.load(open(args.bootstrap))
    domain = boot["domain"]
    tag = str(int(time.time()))

    # local test domain: exempt from the per-domain warm-up quota (a fresh domain starts at a
    # few hundred emails per day, which is exactly what protects a real new domain)
    exec_sql(
        "INSERT INTO domain_volume_ramp (domain_name, state, exempt, daily_quota) VALUES (%s, 'active', 1, 0) "
        "ON DUPLICATE KEY UPDATE exempt = 1",
        (domain,),
    )

    print(f"load test: {args.messages} messages, batch {args.batch}, concurrency {args.concurrency}", flush=True)
    since = time.time()
    sent = asyncio.run(send_all(boot, args.messages, args.batch, args.concurrency, tag))
    print(f"accept: {json.dumps(sent)}", flush=True)

    target = sent["accepted"]
    samples: list[tuple[float, int, int]] = []
    deadline = time.time() + args.timeout
    last_print = 0.0
    while time.time() < deadline:
        c = ledger_counts(domain, since)
        submitted = c.get("smtp_accepted", 0) + c.get("delivered", 0)
        delivered = c.get("delivered", 0)
        now = time.time()
        samples.append((now, submitted, delivered))
        if now - last_print >= 10:
            print(f"  t+{now - since:6.0f}s  queued={c.get('queued', 0)} sending={c.get('sending', 0)} "
                  f"submitted={submitted} delivered={delivered} failed={c.get('failed', 0) + c.get('deadletter', 0)}",
                  flush=True)
            last_print = now
        if delivered >= target:
            break
        # no progress for 120 s: something is stuck; stop and report what arrived
        stalled = [s for s in samples if now - s[0] <= 120]
        if len(samples) > 130 and stalled and stalled[0][2] == delivered and stalled[0][1] == submitted:
            print("  no progress for 120 s, stopping", flush=True)
            break
        time.sleep(1)

    def rate(idx: int) -> tuple[float, float]:
        """(average, best 60 s window) messages/s for column idx of the samples."""
        if len(samples) < 2:
            return 0.0, 0.0
        last = max(samples, key=lambda s: s[idx])
        span = max(last[0] - since, 1e-6)
        avg = last[idx] / span
        best = 0.0
        j = 0
        for i in range(len(samples)):
            while samples[i][0] - samples[j][0] > 60:
                j += 1
            dt = samples[i][0] - samples[j][0]
            if dt >= 20:
                best = max(best, (samples[i][idx] - samples[j][idx]) / dt)
        return round(avg, 1), round(best, 1)

    sub_avg, sub_best = rate(1)
    del_avg, del_best = rate(2)
    final = ledger_counts(domain, since)
    result = {
        "messages": args.messages,
        "accepted": target,
        "accept_per_s": sent["accept_per_s"],
        "api_p50_ms": sent.get("p50_ms"),
        "api_p95_ms": sent.get("p95_ms"),
        "submitted_per_s_avg": sub_avg,
        "submitted_per_s_best_60s": sub_best,
        "delivered_per_s_avg": del_avg,
        "delivered_per_s_best_60s": del_best,
        "end_to_end_seconds": round(samples[-1][0] - since, 1) if samples else None,
        "final_states": final,
    }
    print("RESULT " + json.dumps(result), flush=True)
    return 0 if final.get("delivered", 0) >= target else 1


if __name__ == "__main__":
    raise SystemExit(main())
