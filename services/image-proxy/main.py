"""RevBridge Connect: image proxy for the images in emails.

Fixes the "black logo" in email clients: a transparent PNG gets the background color of the
cell it sits in, and an SVG (which Gmail and Outlook do not show) becomes a PNG. The API wraps
every <img> of the HTML at send time:

    /p?url=<image url>&bg=<hex color>&s=<signature>

The signature is an HMAC-SHA256 (IMAGE_PROXY_SECRET) of the URL and the color: the proxy only
fetches images that this installation put into an email. Without it, it would be an open proxy
(free bandwidth and CPU for anyone, and an open redirect on the installation's domain).

    POST /svg   (the API only, with Authorization: Bearer <IMAGE_PROXY_SECRET>)
        Inline SVG from the HTML -> cached PNG; returns the public URL /svg/<hash>.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import logging
import os
import re
import sys
from typing import Optional, Tuple
from urllib.parse import unquote, urlparse

import cairosvg
import httpx
import redis.asyncio as redis
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from PIL import Image

# ─── configuration ──────────────────────────────────────────────────────────
# Cache in a Redis of its own (the compose redis-cache service, with maxmemory + LRU): the
# send-queue Redis has no eviction, and the image cache must never fill up the queue.
REDIS_URL = os.getenv("IMAGE_CACHE_REDIS_URL") or "redis://redis-cache:6379/0"
CACHE_TTL = int(os.getenv("IMAGE_CACHE_TTL", str(86400 * 7)))
MAX_IMAGE_SIZE = int(os.getenv("IMAGE_PROXY_MAX_BYTES", str(5 * 1024 * 1024)))
MAX_SVG_SIZE = int(os.getenv("IMAGE_PROXY_MAX_SVG_BYTES", str(256 * 1024)))
MAX_PIXELS = int(os.getenv("IMAGE_PROXY_MAX_PIXELS", str(25_000_000)))
FETCH_TIMEOUT = float(os.getenv("IMAGE_PROXY_FETCH_TIMEOUT", "10"))
SECRET = os.getenv("IMAGE_PROXY_SECRET", "")
# Public URL of the proxy (https://img.<domain>): used only in the URL that POST /svg returns
SERVICE_URL = (os.getenv("IMAGE_PROXY_URL") or "").rstrip("/")

# An email image larger than this is abuse; Pillow refuses it (DecompressionBombError) before
# allocating the memory.
Image.MAX_IMAGE_PIXELS = MAX_PIXELS

_HTML_AMP_ENTITY_RE = re.compile(r"&(?:amp;)+")
_HTML_AMP_NUMERIC_RE = re.compile(r"&#(?:0*38|[xX]0*26);")
_BG_RE = re.compile(r"^[0-9A-F]{3}([0-9A-F]{3})?$")

logging.basicConfig(
    stream=sys.stdout,
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}',
)
logger = logging.getLogger("image-proxy")

app = FastAPI(title="RevBridge Connect image proxy", docs_url=None, redoc_url=None, openapi_url=None)


# ─── signature ──────────────────────────────────────────────────────────────
def sign(url: str, bg: str) -> str:
    """Same computation as mail_api.common.image_proxy.sign_proxy_url (keep the two identical)."""
    mac = hmac.new(SECRET.encode(), f"{url}\n{bg.upper()}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac[:16]).decode().rstrip("=")


def _verify(url: str, bg: str, sig: Optional[str]) -> None:
    if not SECRET:
        # Without a secret the proxy serves nothing: it fails closed instead of becoming an open proxy.
        raise HTTPException(503, "image proxy without IMAGE_PROXY_SECRET")
    if not sig or not hmac.compare_digest(sign(url, bg), sig):
        raise HTTPException(403, "invalid signature")


def _verify_bearer(request: Request) -> None:
    if not SECRET:
        raise HTTPException(503, "image proxy without IMAGE_PROXY_SECRET")
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    if not token or not hmac.compare_digest(token, SECRET):
        raise HTTPException(403, "unauthorized")


# ─── safe fetch ─────────────────────────────────────────────────────────────
def _assert_public_url(url: str) -> None:
    """Blocks SSRF: http/https only, and every IP of the host must be public.

    Inside the compose network the proxy can reach the internal services (database, Redis,
    API). Runs on the initial request and on every redirect (httpx event hook).
    """
    import ipaddress
    import socket

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(400, "invalid URL")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror:
        raise HTTPException(400, "host does not resolve")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise HTTPException(403, "destination not allowed")


async def _guard_request(request: httpx.Request) -> None:
    _assert_public_url(str(request.url))


async def fetch_image(url: str) -> Tuple[bytes, str]:
    """Download the image with a timeout and a byte cap (read as a stream: a missing or
    false Content-Length cannot make the proxy hold a huge file in memory)."""
    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT,
        follow_redirects=True,
        max_redirects=5,
        event_hooks={"request": [_guard_request]},
        headers={"User-Agent": "RevBridge-Connect-ImageProxy/1.0"},
    ) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            declared = int(response.headers.get("content-length") or 0)
            if declared > MAX_IMAGE_SIZE:
                raise HTTPException(413, "image too large")
            buf = bytearray()
            async for chunk in response.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_IMAGE_SIZE:
                    raise HTTPException(413, "image too large")
            return bytes(buf), response.headers.get("content-type", "").lower()


# ─── processing ─────────────────────────────────────────────────────────────
def hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def has_transparency(img: Image.Image) -> bool:
    if img.mode in ("RGBA", "LA"):
        alpha = img.getchannel("A")
        return alpha.getextrema()[0] < 255
    if img.mode == "P":
        return "transparency" in img.info
    return False


def apply_background(img: Image.Image, bg_color: Tuple[int, int, int]) -> Image.Image:
    if img.mode == "P":
        img = img.convert("RGBA")
    if img.mode == "LA":
        img = img.convert("RGBA")
    background = Image.new("RGB", img.size, bg_color)
    background.paste(img, mask=img.getchannel("A"))
    return background


def is_svg(data: bytes, content_type: str, url: str) -> bool:
    if "svg" in (content_type or ""):
        return True
    if urlparse(url).path.lower().endswith(".svg"):
        return True
    head = data[:512].lstrip().lower()
    return head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head)


def _no_external_fetch(*_args, **_kwargs):
    raise ValueError("external resources in an SVG are not allowed")


def svg_to_png(svg_data: bytes, bg_color: str, width: Optional[int] = None, height: Optional[int] = None) -> bytes:
    """SVG -> PNG without fetching anything: an <image href="http://..."> or an external XML
    entity would make cairosvg reach the internal network."""
    if len(svg_data) > MAX_SVG_SIZE:
        raise HTTPException(413, "SVG too large")
    return cairosvg.svg2png(
        bytestring=svg_data,
        output_width=width,
        output_height=height,
        background_color=f"#{bg_color}",
        unsafe=False,
        url_fetcher=_no_external_fetch,
    )


def process_image(image_data: bytes, bg_color: str) -> Tuple[bytes, str]:
    """A transparent PNG gets the background; the rest passes through (WebP/GIF/BMP become JPEG)."""
    img = Image.open(io.BytesIO(image_data))
    original_format = img.format or "PNG"
    if has_transparency(img):
        img = apply_background(img, hex_to_rgb(bg_color))
        out = io.BytesIO()
        img.save(out, format="PNG", optimize=True)
        return out.getvalue(), "image/png"
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGB")
        out = io.BytesIO()
        if original_format == "JPEG":
            img.save(out, format="JPEG", quality=90, optimize=True)
            return out.getvalue(), "image/jpeg"
        img.save(out, format="PNG", optimize=True)
        return out.getvalue(), "image/png"
    if original_format not in ("JPEG", "PNG"):
        # email clients and the cache marker only know PNG/JPEG
        out = io.BytesIO()
        img.convert("RGB").save(out, format="JPEG", quality=90, optimize=True)
        return out.getvalue(), "image/jpeg"
    return image_data, f"image/{original_format.lower()}"


# ─── cache ──────────────────────────────────────────────────────────────────
_redis: Optional[redis.Redis] = None


async def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(REDIS_URL, decode_responses=False)
    return _redis


def cache_key(url: str, bg: str) -> str:
    return "img:" + hashlib.sha256(f"{url}:{bg}".encode()).hexdigest()[:32]


def _normalize_bg(bg: str) -> str:
    bg = (bg or "FFFFFF").lstrip("#").upper()
    if not _BG_RE.match(bg):
        raise HTTPException(400, "invalid background color")
    if len(bg) == 3:
        bg = "".join(c * 2 for c in bg)
    return bg


def _clean_url(url: str) -> str:
    # Starlette has already decoded the query once; decode it again only if the client
    # encoded it twice. &amp; entities left over from badly generated HTML become "&".
    if not url.startswith(("http://", "https://")):
        url = unquote(url)
    url = _HTML_AMP_ENTITY_RE.sub("&", url)
    return _HTML_AMP_NUMERIC_RE.sub("&", url)


# ─── routes ─────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "healthy", "signed": bool(SECRET)}


@app.get("/p")
async def proxy_image(
    url: str = Query(..., max_length=4096),
    bg: str = Query("FFFFFF"),
    s: Optional[str] = Query(None, max_length=64),
):
    bg = _normalize_bg(bg)
    _verify(url, bg, s)  # over the URL exactly as the API signed it
    url = _clean_url(url)
    if urlparse(url).scheme not in ("http", "https"):
        raise HTTPException(400, "invalid URL")

    key = cache_key(url, bg)
    try:
        cached = await (await get_redis()).get(key)
        if cached:
            ct = "image/png" if cached[0] == 0 else "image/jpeg"
            return Response(content=cached[1:], media_type=ct, headers={"Cache-Control": "public, max-age=86400"})
    except Exception as e:
        logger.warning(f"cache unavailable: {e}")

    try:
        data, fetched_ct = await fetch_image(url)
        if is_svg(data, fetched_ct, url):
            out, ct = await asyncio.to_thread(svg_to_png, data, bg), "image/png"
        else:
            out, ct = await asyncio.to_thread(process_image, data, bg)
        try:
            marker = b"\x00" if ct == "image/png" else b"\x01"
            await (await get_redis()).setex(key, CACHE_TTL, marker + out)
        except Exception as e:
            logger.warning(f"failed to write the cache: {e}")
        return Response(content=out, media_type=ct, headers={"Cache-Control": "public, max-age=86400"})
    except HTTPException:
        raise
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        # The origin refused the server (WAF, bot challenge) or did not answer; the reader of
        # the email usually gets the image from their own IP. The URL was signed by this
        # installation, so the redirect is not open. 302 (not cached) so it is retried next time.
        logger.info(f"fetch failed ({type(e).__name__}); redirecting to the origin")
        return RedirectResponse(url, status_code=302)
    except Image.DecompressionBombError:
        raise HTTPException(413, "image too large")
    except Exception as e:
        logger.info(f"processing failed ({type(e).__name__}); redirecting to the origin")
        return RedirectResponse(url, status_code=302)


@app.head("/p", include_in_schema=False)
async def proxy_image_head(url: str = Query(..., max_length=4096), bg: str = Query("FFFFFF"), s: Optional[str] = Query(None)):
    """Security gateways and previews send a HEAD before showing the image."""
    resp = await proxy_image(url=url, bg=bg, s=s)
    headers = {"location": resp.headers["location"]} if resp.headers.get("location") else {}
    return Response(status_code=resp.status_code, media_type=resp.media_type, headers=headers)


@app.post("/svg")
async def svg_upload(request: Request):
    """Inline SVG from the HTML -> cached PNG. Only the API calls it (Bearer = IMAGE_PROXY_SECRET)."""
    _verify_bearer(request)
    bg = _normalize_bg(request.query_params.get("bg", "FFFFFF"))
    try:
        w = int(request.query_params["w"]) if request.query_params.get("w") else None
        h = int(request.query_params["h"]) if request.query_params.get("h") else None
    except ValueError:
        raise HTTPException(400, "invalid w/h")
    if (w and not 0 < w <= 4000) or (h and not 0 < h <= 4000):
        raise HTTPException(400, "w/h out of range")
    body = await request.body()
    if not body:
        raise HTTPException(400, "empty body")
    if b"<svg" not in body[:MAX_SVG_SIZE].lower():
        raise HTTPException(400, "the body has no <svg>")
    digest = hashlib.sha256(body + bg.encode()).hexdigest()[:16]
    try:
        png = await asyncio.to_thread(svg_to_png, body, bg, w, h)
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"invalid SVG: {type(e).__name__}")
        raise HTTPException(400, "invalid SVG")
    try:
        await (await get_redis()).setex(f"svg:{digest}", CACHE_TTL, png)
    except Exception as e:
        logger.warning(f"failed to write the SVG cache: {e}")
    return {"url": f"{SERVICE_URL}/svg/{digest}", "hash": digest, "size": len(png)}


@app.get("/svg/{svg_hash}")
async def svg_serve(svg_hash: str):
    if not re.fullmatch(r"[0-9a-f]{16}", svg_hash):
        raise HTTPException(400, "invalid hash")
    try:
        cached = await (await get_redis()).get(f"svg:{svg_hash}")
    except Exception as e:
        logger.warning(f"cache unavailable: {e}")
        cached = None
    if not cached:
        raise HTTPException(404, "SVG not found or expired")
    return Response(content=cached, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.head("/svg/{svg_hash}", include_in_schema=False)
async def svg_serve_head(svg_hash: str):
    resp = await svg_serve(svg_hash)
    return Response(status_code=resp.status_code, media_type=resp.media_type)
