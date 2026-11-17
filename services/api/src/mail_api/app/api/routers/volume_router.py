"""
Volume Router: allowance of the per-domain volume governor (ramp-up).

Shows how much the caller's domain can still send safely today.
See docs/deliverability.md.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from mail_api.common.db import fetch_all_async
from mail_api.services import volume_governor
from mail_api.shared.domain_validation import (
    get_authorized_domain_ids,
    get_user_domain_name_async,
)
from connect_lib.auth.hybrid import require_read_emails_hybrid

logger = logging.getLogger(__name__)

volume_router = APIRouter(prefix="/volume", tags=["volume"])


async def _caller_domains(current_user: dict, domains_param: str | None) -> list[str]:
    """Domains the caller may query: the key's authorized domains (primary +
    api_key_domains); `?domains=` filters within that set.
    """
    ids = await get_authorized_domain_ids(current_user)
    if not ids:
        primary = await get_user_domain_name_async(current_user)
        return [primary] if primary else []
    placeholders = ", ".join(["%s"] * len(ids))
    rows = await fetch_all_async(
        f"SELECT name FROM virtual_domains WHERE id IN ({placeholders})",
        list(ids),
    )
    names = [(r.get("name") or "").lower() for r in rows or [] if r.get("name")]
    if domains_param:
        wanted = {d.strip().lower() for d in domains_param.split(",")}
        names = [n for n in names if n in wanted]
    return names


@volume_router.get(
    "/allowance",
    operation_id="volume_allowance",
    summary="Daily Volume Allowance",
    description=(
        "Healthy daily sending quota of the authenticated domain (reputation "
        "ramp-up), today's consumption and what remains. `governed=false` = the "
        "domain has no curve yet (no limit). `exempt=true` = never held."
    ),
)
async def get_allowance(current_user: dict = Depends(require_read_emails_hybrid)):
    domain_name = await get_user_domain_name_async(current_user)
    return await volume_governor.allowance(domain_name)


@volume_router.get(
    "/health",
    operation_id="volume_health",
    summary="Sending Health Overview",
    description=(
        "Sending health block: the curve state of each of the caller's domains "
        "(ui_state: scaling|cooldown|learning|unmanaged), today's quota, consumption, "
        "tomorrow's projection and the aggregated capacity. `?domains=` narrows it "
        "to some of the caller's domains."
    ),
)
async def get_health(
    domains: str | None = Query(None, description="Comma-separated list of domains"),
    current_user: dict = Depends(require_read_emails_hybrid),
):
    names = await _caller_domains(current_user, domains)
    if not names:
        return {"domains": [], "total_daily_capacity": 0}
    return await volume_governor.health_overview(names)


@volume_router.get(
    "/history",
    operation_id="volume_history",
    summary="Domain Quota History",
    description=(
        "Time series of the governor's curve (init/ramp/brake/resume/decay/cool/"
        "rewarm steps with quota, volume and health), used by the "
        "'Sent vs domain limit' chart."
    ),
)
async def get_history(
    domain: str = Query(..., description="Sending domain"),
    days: int = Query(30, ge=1, le=90),
    current_user: dict = Depends(require_read_emails_hybrid),
):
    d = domain.strip().lower()
    allowed = await _caller_domains(current_user, None)
    if d not in allowed:
        # default-deny with 404 (do not reveal that another customer's domain exists)
        raise HTTPException(status_code=404, detail="Domain not found")
    return {"domain": d, "steps": await volume_governor.quota_history(d, days)}
