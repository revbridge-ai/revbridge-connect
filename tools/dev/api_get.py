"""Authenticated GET against the local API (testing). Usage, inside the `api` container:
    python tools/dev/api_get.py /tmp/bootstrap.json /status/<message_id> [/other/path ...]
"""

import json
import sys

import httpx

boot = json.load(open(sys.argv[1]))
auth = {"Authorization": f"Bearer {boot['api_key']}"}
for path in sys.argv[2:]:
    r = httpx.get("http://127.0.0.1:8001" + path, headers=auth, timeout=15)
    print(f"GET {path} -> {r.status_code}: {r.text[:900]}")
