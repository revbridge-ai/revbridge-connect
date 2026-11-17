"""Tests for gzip support on /postfix and /batch ingest.

The Vector http sink sends `compression = "gzip"` (config/vector/vector.toml).
`_read_ingest_body` decompresses the body when `Content-Encoding: gzip` is set,
and an invalid payload is an HTTP 400, so Vector retries (within its capped
retry window) instead of treating the batch as delivered, and the error shows
up in the access metrics.
"""
from __future__ import annotations

import gzip

import pytest
from fastapi import HTTPException
from mail_milter.events_updater.app import _read_ingest_body
from starlette.requests import Request


def _make_request(body: bytes, headers: dict[str, str]) -> Request:
    """Build a minimal Starlette Request that yields `body` once."""
    scope = {
        "type": "http",
        "method": "POST",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    delivered = {"done": False}

    async def receive() -> dict:
        if delivered["done"]:
            return {"type": "http.disconnect"}
        delivered["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


async def test_read_ingest_body_decompresses_gzip_payload():
    """Vector ships JSON with Content-Encoding: gzip — server must ungzip."""
    original = b'{"queue_id": "ABC123", "rcpt": "user@example.com", "status": "sent"}'
    compressed = gzip.compress(original)

    request = _make_request(compressed, {"content-encoding": "gzip"})
    result = await _read_ingest_body(request)

    assert result == original


async def test_read_ingest_body_400_on_corrupted_gzip():
    """Bad gzip (header lies) must surface as HTTP 400, not a silent 200."""
    not_actually_gzip = b"\x1f\x8b\x08not-a-valid-gzip-stream"

    request = _make_request(not_actually_gzip, {"content-encoding": "gzip"})

    with pytest.raises(HTTPException) as excinfo:
        await _read_ingest_body(request)

    assert excinfo.value.status_code == 400
    assert "invalid_gzip" in excinfo.value.detail
