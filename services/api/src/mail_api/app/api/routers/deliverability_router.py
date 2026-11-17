"""
Deliverability Router

Endpoints for deliverability metrics and trend analysis.
"""

import logging
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from mail_api.common.db import fetch_all, fetch_one
from mail_api.shared.domain_validation import (
    get_associated_subdomain_id,
    get_user_domain_id,
    get_user_domain_name,
)
from connect_lib.auth.hybrid import require_read_stats_hybrid

from ..utils import (
    AVG_DELIVERY_TIME_SELECT_BASE,
    BOUNCE_BREAKDOWN_SELECT,
    DELIVERY_COUNTS_SELECT,
    ISP_PERFORMANCE_SELECT,
    TIMESERIES_SELECT,
    calculate_rate,
    determine_trend,
    log_operation_error,
    log_operation_start,
    log_operation_success,
    parse_date_range,
    process_bounce_breakdown,
    process_isp_performance,
    process_timeseries_data,
)

logger = logging.getLogger(__name__)

# Router for the deliverability endpoints
deliverability_router = APIRouter(prefix="/deliverability", tags=["deliverability"])


def get_user_subdomain_name(current_user: dict) -> str:
    """
    Returns the domain_name of the authenticated user's subdomain.
    If there is no subdomain, returns the user's main domain_name.

    Args:
        current_user: Authenticated user data (from the JWT)

    Returns:
        str: Name of the subdomain or of the main domain

    Raises:
        HTTPException: If the user is not associated with a domain
    """
    # The user's domain_id
    user_domain_id = get_user_domain_id(current_user)
    if not user_domain_id:
        raise HTTPException(
            status_code=403,
            detail="User is not associated with a domain"
        )

    # domain_id of the associated subdomain
    subdomain_id = get_associated_subdomain_id(user_domain_id)

    # No subdomain: use the main domain_name
    if not subdomain_id:
        return get_user_domain_name(current_user)

    # Fetch the subdomain's domain_name
    try:
        result = fetch_one(
            "SELECT name FROM virtual_domains WHERE id = %s",
            (subdomain_id,)
        )
        if not result:
            # Fall back to the main domain_name
            return get_user_domain_name(current_user)
        return result.get("name")
    except Exception as e:
        logger.warning(f"Error fetching subdomain_name: {e}, using the main domain_name")
        # Fall back to the main domain_name
        return get_user_domain_name(current_user)


def append_email_filter(sql: str, email: str | None) -> str:
    """Appends the optional recipient (rcpt) filter."""
    if email:
        # appends a simple AND rcpt = %s
        if "WHERE" in sql:
            return sql + " AND rcpt = %s"
    return sql


def add_email_param(params: tuple, email: str | None) -> tuple:
    """Appends the email to the params when present."""
    if email:
        return params + (email,)
    return params


def _to_date(d: str | date) -> date:
    if isinstance(d, date):
        return d
    return datetime.strptime(d, "%Y-%m-%d").date()


def _date_iso(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def _split_period_in_halves(
    start_d: date, end_d: date
) -> tuple[date, date, date, date]:
    # NON-overlapping intervals: [start, mid] and [mid+1, end]
    if end_d < start_d:
        raise ValueError("end_date < start_date")
    total_days = (end_d - start_d).days + 1  # inclusive
    half = total_days // 2
    first_end = start_d + timedelta(days=half - 1) if half > 0 else start_d
    second_start = first_end + timedelta(days=1)
    if second_start > end_d:  # single-day period
        second_start = first_end  # both halves point to the same day
    return start_d, first_end, second_start, end_d


@deliverability_router.get(
    "/",
    summary="Get Deliverability Metrics",
    description="""
## Get Deliverability Metrics

Retrieve detailed deliverability metrics with trend analysis.

### Metrics Included

**Overall Metrics**
- Total sent, delivered, bounced, deferred, failed
- Delivery rate and bounce rate percentages
- Average delivery time in minutes

**Bounce Breakdown**
- Hard bounces (permanent failures)
- Soft bounces (temporary failures)
- Policy bounces (rejected by policy)
- Other bounces

**ISP Performance**
- Delivery rates by email provider (Gmail, Outlook, etc.)
- Top 20 ISPs by volume

**Time Series**
- Daily metrics over the selected period

**Trend Analysis**
- Period comparison (first half vs second half)
- Percentage point changes
- Trend indicators (improving, stable, declining)

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `start_date` | string | 30 days ago | Start date (YYYY-MM-DD) |
| `end_date` | string | today | End date (YYYY-MM-DD) |
| `email` | string | - | Filter by specific recipient email |

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {
            "description": "Deliverability metrics retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "status": "ok",
                        "domain_name": "example.com",
                        "period": {
                            "start_date": "2025-01-01",
                            "end_date": "2025-01-31",
                        },
                        "filters": {"email": None},
                        "overall_metrics": {
                            "total_sent": 10000,
                            "total_delivered": 9500,
                            "total_bounced": 300,
                            "total_deferred": 150,
                            "total_failed": 50,
                            "delivery_rate": 95.0,
                            "bounce_rate": 3.0,
                            "avg_delivery_time_minutes": 2.5,
                        },
                        "bounce_breakdown": {
                            "hard_bounces": 100,
                            "soft_bounces": 150,
                            "policy_bounces": 30,
                            "other_bounces": 20,
                        },
                        "isp_performance": [
                            {
                                "isp": "gmail.com",
                                "sent": 5000,
                                "delivered": 4800,
                                "delivery_rate": 96.0,
                            }
                        ],
                        "timeseries": [
                            {
                                "date": "2025-01-01",
                                "sent": 1000,
                                "delivered": 950,
                                "bounced": 30,
                            }
                        ],
                        "trend_analysis": {
                            "trend": "improving",
                            "trend_description": "Deliverability improving",
                            "delivery_rate_change_pp": 2.5,
                        },
                    }
                }
            },
        },
        401: {
            "description": "Invalid or expired token",
            "content": {
                "application/json": {
                    "example": {"detail": "Invalid or expired token"}
                }
            },
        },
        500: {
            "description": "Internal server error",
            "content": {
                "application/json": {"example": {"detail": "Internal server error"}}
            },
        },
    },
    tags=["deliverability"],
    operation_id="deliverability_stats",
)
async def get_deliverability_stats(
    start_date: str | None = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: str | None = Query(None, description="End date (YYYY-MM-DD)"),
    current_user: dict = Depends(require_read_stats_hybrid),
    email: str | None = Query(None, description="Filter by specific email address"),
):
    """
    Returns detailed deliverability metrics for the user's domain,
    with an optional recipient (email) filter.
    """
    try:
        # domain_name of the user's subdomain
        domain_name = get_user_subdomain_name(current_user)

        # 0) Normalized dates
        start_date, end_date = parse_date_range(start_date, end_date)
        log_operation_start(
            "DELIVERABILITY",
            domain_name=domain_name,
            start_date=start_date,
            end_date=end_date,
            email=email,
        )

        # --------- 1) OVERALL COUNTS ----------
        counts_sql = append_email_filter(DELIVERY_COUNTS_SELECT, email)
        counts_params = add_email_param((domain_name, start_date, end_date), email)
        overall_counts = fetch_one(counts_sql, counts_params) or {}

        total_sent = overall_counts.get("total_sent", 0) or 0
        total_delivered = overall_counts.get("total_delivered", 0) or 0
        total_bounced = overall_counts.get("total_bounced", 0) or 0
        total_deferred = overall_counts.get("total_deferred", 0) or 0
        total_failed = overall_counts.get("total_failed", 0) or 0

        delivery_rate = calculate_rate(total_delivered, total_sent)
        bounce_rate = calculate_rate(total_bounced, total_sent)

        # ------ 1b) AVG DELIVERY TIME (min) ------
        # Parameter order: (start_date, end_date, domain_name[, email])
        maybe_email_clause = "AND d.rcpt = %s" if email else ""
        avg_sql = AVG_DELIVERY_TIME_SELECT_BASE.format(
            maybe_email_clause=maybe_email_clause
        )
        avg_params = (start_date, end_date, domain_name)
        if email:
            avg_params = avg_params + (email,)
        avg_row = fetch_one(avg_sql, avg_params) or {}
        avg_delivery_min = avg_row.get("avg_delivery_time_minutes", 0) or 0.0

        # --------- 2) BOUNCE BREAKDOWN ----------
        bb_sql = append_email_filter(BOUNCE_BREAKDOWN_SELECT, email)
        bb_params = add_email_param((domain_name, start_date, end_date), email)
        bounce_breakdown = fetch_one(bb_sql, bb_params) or {
            "hard_bounces": 0,
            "policy_bounces": 0,
            "soft_bounces": 0,
            "other_bounces": 0,
        }
        processed_bounce_breakdown = process_bounce_breakdown(
            bounce_breakdown, total_sent
        )

        # --------- 3) ISP PERFORMANCE ----------
        isp_sql = append_email_filter(ISP_PERFORMANCE_SELECT, email)
        isp_sql += " GROUP BY SUBSTRING_INDEX(d.rcpt, '@', -1) HAVING sent >= 10 ORDER BY sent DESC LIMIT 20"
        isp_params = add_email_param((domain_name, start_date, end_date), email)
        isp_rows = fetch_all(isp_sql, isp_params)
        isp_data = process_isp_performance(isp_rows)

        # --------- 4) TIMESERIES (daily) --------
        ts_sql = append_email_filter(TIMESERIES_SELECT, email)
        ts_sql += " GROUP BY date ORDER BY date"
        ts_params = add_email_param((domain_name, start_date, end_date), email)
        ts_rows = fetch_all(ts_sql, ts_params)
        timeseries = process_timeseries_data(ts_rows)

        # --------- 5) Trend analysis --------
        trend_analysis = await analyze_deliverability_trends(
            domain_name, start_date, end_date, email
        )

        log_operation_success(
            "DELIVERABILITY",
            total_sent=total_sent,
            total_delivered=total_delivered,
            delivery_rate=delivery_rate,
        )

        return {
            "status": "ok",
            "domain_name": domain_name,
            "period": {"start_date": start_date, "end_date": end_date},
            "filters": {"email": email},
            "overall_metrics": {
                "total_sent": total_sent,
                "total_delivered": total_delivered,
                "total_bounced": total_bounced,
                "total_deferred": total_deferred,
                "total_failed": total_failed,
                "delivery_rate": delivery_rate,
                "bounce_rate": bounce_rate,
                "avg_delivery_time_minutes": round(avg_delivery_min, 1),
            },
            "bounce_breakdown": processed_bounce_breakdown,
            "isp_performance": isp_data,
            "timeseries": timeseries,
            "trend_analysis": trend_analysis,
            "timestamp": datetime.now().isoformat(),
        }

    except Exception as e:
        log_operation_error("DELIVERABILITY", e, domain_name=domain_name, email=email)
        raise HTTPException(
            status_code=500, detail="Error building the deliverability metrics"
        ) from e


async def analyze_deliverability_trends(
    domain_name: str,
    start_date: str,  # may be a string
    end_date: str,  # may be a string
    email: str | None = None,
) -> dict:
    try:
        # 1) Normalize to date
        s_d = _to_date(start_date)
        e_d = _to_date(end_date)

        # 2) Split into halves (as dates)
        fh_start_d, fh_end_d, sh_start_d, sh_end_d = _split_period_in_halves(s_d, e_d)

        # 3) Convert each half to ISO (for SQL)
        fh_start, fh_end = _date_iso(fh_start_d), _date_iso(fh_end_d)
        sh_start, sh_end = _date_iso(sh_start_d), _date_iso(sh_end_d)

        # 4) Base counts query
        counts_sql = append_email_filter(DELIVERY_COUNTS_SELECT, email)

        fh_params = add_email_param((domain_name, fh_start, fh_end), email)
        sh_params = add_email_param((domain_name, sh_start, sh_end), email)

        first_half = fetch_one(counts_sql, fh_params) or {}
        second_half = fetch_one(counts_sql, sh_params) or {}

        if not first_half and not second_half:
            return {"trend": "insufficient_data", "message": "No events in the period"}

        fh_sent = first_half.get("total_sent") or 0
        fh_delivered = first_half.get("total_delivered") or 0
        fh_bounced = first_half.get("total_bounced") or 0

        sh_sent = second_half.get("total_sent") or 0
        sh_delivered = second_half.get("total_delivered") or 0
        sh_bounced = second_half.get("total_bounced") or 0

        MIN_BASE = 5
        if fh_sent < MIN_BASE or sh_sent < MIN_BASE:
            return {
                "trend": "insufficient_data",
                "message": f"Not enough volume for the analysis (fh_sent={fh_sent}, sh_sent={sh_sent})",
                "periods": {
                    "first_half": {"start": fh_start, "end": fh_end, "sent": fh_sent},
                    "second_half": {"start": sh_start, "end": sh_end, "sent": sh_sent},
                },
            }

        fh_delivery_rate = calculate_rate(fh_delivered, fh_sent)
        sh_delivery_rate = calculate_rate(sh_delivered, sh_sent)
        fh_bounce_rate = calculate_rate(fh_bounced, fh_sent)
        sh_bounce_rate = calculate_rate(sh_bounced, sh_sent)

        delivery_change = calculate_change(sh_delivery_rate, fh_delivery_rate)  # pp
        bounce_change = calculate_change(sh_bounce_rate, fh_bounce_rate)

        trend_info = determine_trend(delivery_change)

        return {
            "trend": trend_info["trend"],
            "trend_description": trend_info["description"],
            "periods": {
                "first_half": {
                    "start": fh_start,
                    "end": fh_end,
                    "sent": fh_sent,
                    "delivered": fh_delivered,
                    "bounced": fh_bounced,
                    "delivery_rate": round(fh_delivery_rate, 2),
                    "bounce_rate": round(fh_bounce_rate, 2),
                },
                "second_half": {
                    "start": sh_start,
                    "end": sh_end,
                    "sent": sh_sent,
                    "delivered": sh_delivered,
                    "bounced": sh_bounced,
                    "delivery_rate": round(sh_delivery_rate, 2),
                    "bounce_rate": round(sh_bounce_rate, 2),
                },
            },
            "changes": {
                "delivery_rate_change_pp": round(delivery_change, 2),
                "bounce_rate_change_pp": round(bounce_change, 2),
            },
            "filters": {"email": email},
        }

    except Exception as e:
        log_operation_error("DELIVERABILITY_TRENDS", e, domain_name=domain_name)
        return {"trend": "error", "message": f"Analysis error: {e!s}"}


def calculate_change(current: float, previous: float) -> float:
    """Change between two percentages, in percentage points"""
    return current - previous
