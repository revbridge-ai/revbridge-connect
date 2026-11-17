"""Webhook receiver for local testing (webhook-sink service in docker-compose.dev.yml).

POST to any path: stores the path, headers and body. Answers 200, except on paths that
end in /fail (500) and /bad (400), to test retries.
GET /received[?path=/a]: lists what arrived. POST /reset: clears it.
Standard library only. Never in production.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

RECEIVED: list[dict] = []
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict | list) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        path = urlsplit(self.path).path
        if path == "/reset":
            with LOCK:
                RECEIVED.clear()
            return self._send(200, {"ok": True})
        with LOCK:
            RECEIVED.append({"path": path, "headers": dict(self.headers.items()), "body": body})
        if path.endswith("/fail"):
            return self._send(500, {"error": "simulated failure"})
        if path.endswith("/bad"):
            return self._send(400, {"error": "simulated rejection"})
        return self._send(200, {"ok": True})

    def do_GET(self):  # noqa: N802
        parts = urlsplit(self.path)
        if parts.path != "/received":
            return self._send(404, {"error": "not found"})
        want = parse_qs(parts.query).get("path", [None])[0]
        with LOCK:
            items = [r for r in RECEIVED if want is None or r["path"] == want]
        return self._send(200, items)

    def log_message(self, fmt, *args):  # silent
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8099), Handler).serve_forever()
