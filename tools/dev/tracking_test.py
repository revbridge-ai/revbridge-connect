"""Local test of tracking and reports.

Runs INSIDE the `api` container:
    docker compose exec api python tools/dev/tracking_test.py /tmp/bootstrap.json

1. Takes the latest email in Mailpit (compose network) and extracts the pixel and the click link.
2. Opens the pixel and clicks the link on the tracking service, like a browser.
3. Queries the API reports with the tenant's API key.
Local testing only.
"""

from __future__ import annotations

import json
import re
import sys
import time

import httpx

MAILPIT = "http://mailpit:8025"
TRACKING = "http://tracking:8002"
API = "http://127.0.0.1:8001"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")
# a documentation address (RFC 5737) outside every cloud range: bot detection keeps the event
CLIENT_IP = "203.0.113.20"


def main() -> int:
    boot = json.load(open(sys.argv[1]))
    auth = {"Authorization": f"Bearer {boot['api_key']}"}
    msgs = httpx.get(f"{MAILPIT}/api/v1/messages", timeout=10).json()["messages"]
    msg = httpx.get(f"{MAILPIT}/api/v1/message/{msgs[0]['ID']}", timeout=10).json()
    html = msg.get("HTML", "")
    pixel = re.search(r'src="https?://[^/"]+(/o/[^"]+\.png)"', html)
    click = re.search(r'href="https?://[^/"]+(/c/[^"]+)"', html)
    print("pixel:", bool(pixel), "| click:", bool(click))
    hdrs = {"User-Agent": BROWSER_UA, "X-Forwarded-For": CLIENT_IP}

    # the open gate ignores opens less than OPEN_MIN_DELTA_SECONDS after delivery
    time.sleep(5)
    if pixel:
        r = httpx.get(TRACKING + pixel.group(1), headers=hdrs, timeout=10)
        print("open ->", r.status_code, r.headers.get("content-type"), len(r.content), "bytes")
    if click:
        r = httpx.get(TRACKING + click.group(1), headers=hdrs, timeout=10, follow_redirects=False)
        print("click ->", r.status_code, "Location:", r.headers.get("location"))

    time.sleep(3)
    for path in ("/stats/dashboard", "/stats/messages", "/deliverability/"):
        r = httpx.get(API + path, headers=auth, timeout=15)
        body = r.text
        print(f"GET {path} -> {r.status_code}: {body[:700]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
