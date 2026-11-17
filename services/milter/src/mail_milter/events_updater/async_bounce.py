"""Decoding of ASYNCHRONOUS bounces (a late DSN delivered to the VERP address).

The synchronous path (process_delivery_event) suppresses hard bounces from the
Postfix log. Failures after acceptance arrive LATER, as a DSN email (RFC 3464)
addressed to the VERP Return-Path `bounces+<rcpt>=<dom>@rbm<n>.<...>`; the MTA
hands it to the events-updater's `/bounces/async` endpoint. (The Postfix log path
skips these messages on purpose: `rcpt.startswith("bounces+")`.)

This module extracts the original recipient and the status from the VERP envelope
and the DSN body, to feed the SAME suppression rule. Pure functions only (no
I/O): the endpoint in app.py does the glue and the database writes.

The suppression's `domain_name` is the envelope's rbm domain (suppressions and
delivery_events are keyed by the rbm domain, e.g. `rbm100001.m.shop.example.com`),
so NO reverse lookup is needed.
"""
from __future__ import annotations

import re
from email import message_from_bytes
from email.message import Message

# rbm domain: "rbm" + digits + "." + rest (mirrors the `@rbm\d+` filter in app.py).
_RBM_DOMAIN_RE = re.compile(r"^rbm\d+\.", re.IGNORECASE)
_STATUS_RE = re.compile(r"([245]\.\d{1,3}\.\d{1,3})")
_STATUS_LINE_RE = re.compile(rb"^Status:\s*([245]\.\d{1,3}\.\d{1,3})", re.IGNORECASE | re.MULTILINE)


def decode_verp_recipient(envelope_rcpt: str) -> tuple[str, str] | None:
    """Decode the bounce VERP envelope.

    `bounces+ana.silva=mail.example.org@rbm100001.m.shop.example.com`
        -> ("ana.silva@mail.example.org", "rbm100001.m.shop.example.com")

    The encoding replaces the recipient's single `@` with `=` (milter_server
    `rcpt.replace('@','=')`), so we decode at the LAST `=` (the domain never
    contains `=`; a `+` in the recipient's local part is preserved).

    Returns (rcpt, rbm_domain) in lowercase, or None if it is not a valid rbm VERP.
    """
    if not envelope_rcpt:
        return None
    s = envelope_rcpt.strip().strip("<>").strip()
    if not s.lower().startswith("bounces+"):
        return None
    local_part, sep, rbm_domain = s.rpartition("@")
    if not sep:
        return None
    rbm_domain = rbm_domain.lower().strip()
    if not _RBM_DOMAIN_RE.match(rbm_domain):
        return None
    encoded = local_part[len("bounces+"):]
    rcpt_local, eq, rcpt_domain = encoded.rpartition("=")
    if not eq or not rcpt_local or not rcpt_domain:
        return None
    if "." not in rcpt_domain:  # the recipient domain must contain a dot
        return None
    return f"{rcpt_local}@{rcpt_domain}".lower().strip(), rbm_domain


def _clean_status(raw_status: str) -> str:
    """Extract the `d.d.d` code from a Status field (may have extra text)."""
    if not raw_status:
        return ""
    m = _STATUS_RE.search(raw_status)
    return m.group(1) if m else ""


def extract_dsn_status(raw: bytes) -> str:
    """Read the status code (e.g. '5.1.1') of an RFC 3464 DSN.

    Prefers the per-recipient block with `Action: failed`. Falls back to a regex
    over the raw body if MIME parsing finds no `message/delivery-status` part.
    Returns '' if no reliable status is found: the caller must NOT suppress in
    that case (avoids over-suppression from a malformed DSN).
    """
    if not raw:
        return ""
    try:
        msg = message_from_bytes(raw)
    except Exception:
        msg = None

    if msg is not None:
        candidates: list[tuple[str, str]] = []  # (action, status)
        for part in msg.walk():
            if part.get_content_type() != "message/delivery-status":
                continue
            payload = part.get_payload()
            if not isinstance(payload, list):
                continue
            for block in payload:
                if not isinstance(block, Message):
                    continue
                status = _clean_status((block.get("Status") or "").strip())
                if not status:
                    continue
                action = (block.get("Action") or "").strip().lower()
                candidates.append((action, status))
        for action, status in candidates:
            if action == "failed":
                return status
        if candidates:
            return candidates[0][1]

    m = _STATUS_LINE_RE.search(raw)
    if m:
        return m.group(1).decode("ascii", "ignore")
    return ""


_ORIG_ID_LINE_RE = re.compile(rb"^(X-Connect-ID|Message-ID):[ \t]*<?([^\r\n<>]+)>?", re.IGNORECASE | re.MULTILINE)
_UUIDISH = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def extract_original_message_ids(raw: bytes) -> list[str]:
    """IDs of the ORIGINAL message quoted in the DSN (message/rfc822 or text/rfc822-headers part).

    Reads X-Connect-ID and the local part of the Message-ID (the milter generates both
    from the send_ledger id). Ignores the DSN's own Message-ID (top-level header). UUID-shaped
    IDs come first. Empty list when the DSN does not include the original headers.
    """
    if not raw:
        return []
    ids: list[str] = []

    def _add(value: str) -> None:
        value = value.strip().strip("<>").strip()
        if not value:
            return
        for cand in (value.split("@", 1)[0], value):
            if cand and cand not in ids:
                ids.append(cand)

    found_part = False
    try:
        msg = message_from_bytes(raw)
    except Exception:
        msg = None
    if msg is not None:
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype not in ("message/rfc822", "text/rfc822-headers"):
                continue
            payload = part.get_payload()
            original = None
            if isinstance(payload, list) and payload and isinstance(payload[0], Message):
                original = payload[0]
            elif isinstance(payload, Message):
                original = payload
            if original is not None:
                found_part = True
                for header in ("X-Connect-ID", "Message-ID"):
                    if original.get(header):
                        _add(str(original.get(header)))
                continue
            text = part.get_payload(decode=True) or b""
            for _, value in _ORIG_ID_LINE_RE.findall(text):
                found_part = True
                _add(value.decode("utf-8", "replace"))
    if not found_part:
        # DSN without MIME parts: only what appears AFTER the first header block counts
        # (the top block is the DSN's own header)
        _, _, body = raw.partition(b"\r\n\r\n") if b"\r\n\r\n" in raw else raw.partition(b"\n\n")
        for _, value in _ORIG_ID_LINE_RE.findall(body):
            _add(value.decode("utf-8", "replace"))
    return [i for i in ids if _UUIDISH.match(i)] + [i for i in ids if not _UUIDISH.match(i)]


_DIAG_LINE_RE = re.compile(rb"^Diagnostic-Code:([^\r\n]*(?:\r?\n[ \t][^\r\n]*)*)", re.IGNORECASE | re.MULTILINE)


def extract_dsn_diagnostic(raw: bytes) -> str:
    """Text of the DSN's `Diagnostic-Code` field(s) (the remote MX's reply).

    Only that field, NOT the whole body: the DSN carries the original message, and
    content that mentions "blocklist" must not become a reputation-rejection signal.
    Returns '' if there is no Diagnostic-Code.
    """
    if not raw:
        return ""
    return " ".join(
        " ".join(m.decode("utf-8", "replace").split())
        for m in _DIAG_LINE_RE.findall(raw)
    )
