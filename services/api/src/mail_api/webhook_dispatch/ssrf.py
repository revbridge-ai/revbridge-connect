"""SSRF protection for webhook URLs.

On a single-VM installation, the dispatcher can reach the database, Redis and the internal
APIs. A webhook URL (or one of its redirects) pointing there would turn into internal access
on behalf of the wrong client. Rule: only http/https to a public IP. It applies at
registration (API) and on EVERY delivery request, redirects included (httpx request hook).

WEBHOOK_ALLOW_PRIVATE_HOSTS: host names allowed even when they resolve to an internal IP. Only
for local testing (docker-compose.dev.yml); never in production.

Known limit: DNS can change between this check and the connection (rebinding). Connecting to
the validated IP would close it; for now the window is a few milliseconds.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from urllib.parse import urlsplit


class BlockedDestination(Exception):
    """A webhook URL that cannot be called (scheme, host or internal IP)."""


def _allowed_private_hosts() -> set[str]:
    return {h.strip().lower() for h in os.getenv("WEBHOOK_ALLOW_PRIVATE_HOSTS", "").split(",") if h.strip()}


def _is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%", 1)[0])
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


def check_url(url: str) -> None:
    """Raises BlockedDestination if the URL cannot be called. Blocking (resolves DNS)."""
    parts = urlsplit(str(url))
    if parts.scheme not in ("http", "https"):
        raise BlockedDestination(f"scheme not allowed: {parts.scheme or '(empty)'}")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise BlockedDestination("URL without a host")
    if parts.username or parts.password:
        raise BlockedDestination("URLs with a username/password are not accepted")
    if host in _allowed_private_hosts():
        return
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as e:
        raise BlockedDestination("invalid port") from e
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as e:
        raise BlockedDestination(f"host does not resolve: {host}") from e
    if not infos:
        raise BlockedDestination(f"host does not resolve: {host}")
    for info in infos:
        ip = info[4][0]
        if not _is_public(ip):
            raise BlockedDestination(f"internal destination blocked: {host} -> {ip}")


async def check_url_async(url: str) -> None:
    await asyncio.get_running_loop().run_in_executor(None, check_url, str(url))


async def guard_request(request) -> None:
    """httpx request hook: runs before every request, redirects included."""
    await check_url_async(str(request.url))
