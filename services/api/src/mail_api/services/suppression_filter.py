"""
Suppression filter at send time.

The `suppressions` table is written by the unsubscribe link (tracking) and by the
bounce handler (events updater). This module reads it before sending: it is the
gate in the API, the last point EVERY send goes through (transactional mail,
direct API calls, scheduled jobs).

Scoped per sender (not global): the table's unique key is (email, domain_name),
and an unsubscribe from one sender must not silence another sender.
See `_domain_matches` for the matching within a domain family.
"""

import logging
from collections.abc import Iterable

from mail_api.common.config import SUPPRESSION_CHECK_CHUNK, SUPPRESSION_CHECK_MODE
from mail_api.common.db import fetch_all_async

logger = logging.getLogger(__name__)

# Modes: "off" (no lookup), "dryrun" (looks up and logs, does not block),
# "enforce" (looks up and removes the recipient from the send).
MODE_OFF = "off"
MODE_DRYRUN = "dryrun"
MODE_ENFORCE = "enforce"


def _domain_matches(sup_domain: str, candidates: Iterable[str]) -> bool:
    """
    Matches the suppression's domain against the sender domains of this send.

    The suppression is recorded with the ENVELOPE domain (`rbm1001.mail.example.com`,
    which is what the events updater reads from the Postfix log), while the API only
    knows the domain of the JWT / of the From (`mail.example.com`). That is why the
    match accepts a suffix in both directions: an `rbm<id>.X` suppresses a send from
    `X`, and a suppression on `X` applies to a send from `news.X`.

    It is still a match WITHIN the domain family, never across customers.
    """
    sup_domain = (sup_domain or "").strip().lower()
    if not sup_domain:
        return False
    for cand in candidates:
        if sup_domain == cand:
            return True
        if sup_domain.endswith("." + cand) or cand.endswith("." + sup_domain):
            return True
    return False


def sender_domains(domain_name: str | None, sender_email: str | None) -> set[str]:
    """Domains that identify the sender of this send (JWT + From)."""
    out: set[str] = set()
    if domain_name:
        out.add(domain_name.strip().lower())
    if sender_email and "@" in sender_email:
        out.add(sender_email.rsplit("@", 1)[1].strip().lower())
    return {d for d in out if d}


async def lookup_suppressed(
    emails: Iterable[str], domains: set[str]
) -> dict[str, str]:
    """
    Returns {normalized_email: suppression_type} for the addresses suppressed
    for THIS sender. Addresses without a suppression are not in the dict.

    A database error does not block the send (fail-open): suppression is a
    quality/compliance filter, not a security gate, and dropping the whole send
    because MySQL blinked would be worse than letting one more email through. The
    error is logged as ops so it can raise an alert.
    """
    normalized = sorted({e.strip().lower() for e in emails if e and "@" in e})
    if not normalized or not domains:
        return {}

    suppressed: dict[str, str] = {}
    for i in range(0, len(normalized), SUPPRESSION_CHECK_CHUNK):
        chunk = normalized[i : i + SUPPRESSION_CHECK_CHUNK]
        placeholders = ", ".join(["%s"] * len(chunk))
        # Anchored on idx_suppressions_email_active (email, is_active); the domain
        # match is done in Python because LIKE '%.dom' does not use the index and
        # the set of rows per email is already minimal.
        query = f"""
            SELECT email, domain_name, suppression_type
            FROM suppressions
            WHERE is_active = 1
              AND (expires_at IS NULL OR expires_at > NOW())
              AND email IN ({placeholders})
        """
        try:
            rows = await fetch_all_async(query, chunk)
        except Exception as e:
            logger.error(
                "log_type=ops action=suppression_lookup_failed "
                "emails=%d domains=%s error=%s (fail-open: the send goes on)",
                len(chunk), sorted(domains), e,
            )
            continue

        for row in rows or []:
            if _domain_matches(row.get("domain_name"), domains):
                email = (row.get("email") or "").strip().lower()
                if email:
                    suppressed[email] = row.get("suppression_type") or "unknown"

    return suppressed


async def filter_personalizations(
    personalizations: list, domains: set[str]
) -> tuple[list, dict[str, str]]:
    """
    Removes the suppressed recipients from the personalizations.

    Returns (kept_personalizations, {email: suppression_type removed}).
    A personalization left without recipients is dropped.

    In "off" or "dryrun" mode NOTHING is removed: the dict of removed addresses
    is returned for the caller to log/measure, and the list comes back intact.
    """
    if SUPPRESSION_CHECK_MODE == MODE_OFF:
        return personalizations, {}

    all_emails = [
        r.address for p in personalizations for r in p.to if getattr(r, "address", None)
    ]
    suppressed = await lookup_suppressed(all_emails, domains)
    if not suppressed or SUPPRESSION_CHECK_MODE != MODE_ENFORCE:
        return personalizations, suppressed

    kept = []
    for p in personalizations:
        remaining = [
            r for r in p.to if (r.address or "").strip().lower() not in suppressed
        ]
        if not remaining:
            continue
        if len(remaining) != len(p.to):
            p = p.model_copy(update={"to": remaining})
        kept.append(p)

    return kept, suppressed
