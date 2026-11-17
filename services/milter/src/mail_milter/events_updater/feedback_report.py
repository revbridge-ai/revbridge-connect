"""Parsing of complaint reports (ARF, RFC 5965).

Providers with a feedback-loop program (Yahoo, Microsoft, Comcast...) send a
`multipart/report; report-type=feedback-report` email to the registered address. In
Connect that address is `bounces+fbl@rbm<n>.<domain>`: the MTA accepts `bounces+*@rbm*`
and hands it to the bounce pipe (POST /bounces/async), where the report is recognized by
its content.

Pure functions (no I/O): the events-updater checks the send in the database before
suppressing.
"""

from __future__ import annotations

import ipaddress
import re
from email import message_from_bytes, policy
from email.message import Message
from email.parser import HeaderParser
from email.utils import getaddresses

_VERP_DOMAIN = re.compile(r"@(rbm[0-9]+\.[^@>\s]+)", re.IGNORECASE)
_UUIDISH = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def _headers_of(part: Message) -> Message | None:
    """Headers of a message/* part (list with 1 Message) or a text/rfc822-headers part (text)."""
    payload = part.get_payload()
    if isinstance(payload, list) and payload and isinstance(payload[0], Message):
        return payload[0]
    if isinstance(payload, Message):
        return payload
    text = part.get_payload(decode=True)
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    if isinstance(text, str) and text.strip():
        return HeaderParser().parsestr(text)
    return None


def _addr(value: str | None) -> str | None:
    if not value:
        return None
    pairs = getaddresses([value])
    for _name, addr in pairs:
        if addr and "@" in addr:
            return addr.strip().strip("<>").lower()
    return None


def _ip(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def parse_feedback_report(raw: bytes) -> dict | None:
    """ARF fields, or None if the email is not a complaint report."""
    msg = message_from_bytes(raw, policy=policy.compat32)
    if msg.get_content_type() != "multipart/report":
        return None
    if (msg.get_param("report-type") or "").lower() != "feedback-report":
        return None
    report: Message | None = None
    original: Message | None = None
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype == "message/feedback-report" and report is None:
            report = _headers_of(part)
        elif ctype in ("message/rfc822", "text/rfc822-headers") and original is None:
            original = _headers_of(part)
    if report is None:
        return None

    rcpt = _addr(report.get("Original-Rcpt-To")) or (_addr(original.get("To")) if original is not None else None)
    ids: list[str] = []
    if original is not None:
        rid = (original.get("X-Connect-ID") or "").strip()
        if rid:
            ids.append(rid)
        mid = (original.get("Message-ID") or "").strip().strip("<>")
        if mid:
            local = mid.split("@", 1)[0]
            for cand in (local, mid):
                if cand and cand not in ids:
                    ids.append(cand)
    verp_domain = None
    for source in (report.get("Original-Mail-From"), original.get("Return-Path") if original is not None else None):
        m = _VERP_DOMAIN.search(source or "")
        if m:
            verp_domain = m.group(1).lower().rstrip(".")
            break
    from_domain = None
    if original is not None:
        frm = _addr(original.get("From"))
        if frm:
            from_domain = frm.split("@", 1)[1]
    return {
        "feedback_type": (report.get("Feedback-Type") or "abuse").strip().lower()[:50],
        "user_agent": (report.get("User-Agent") or "").strip()[:255] or None,
        "source_ip": _ip(report.get("Source-IP")),
        "arrival_date": (report.get("Arrival-Date") or report.get("Received-Date") or "").strip() or None,
        "reported_domain": (report.get("Reported-Domain") or "").strip().lower() or None,
        "rcpt": rcpt,
        "message_ids": [i for i in ids if _UUIDISH.match(i)] + [i for i in ids if not _UUIDISH.match(i)],
        "verp_domain": verp_domain,
        "from_domain": from_domain,
    }
