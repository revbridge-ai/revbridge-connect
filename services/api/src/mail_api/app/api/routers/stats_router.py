"""
Statistics Router

Endpoints for email statistics, dashboard analytics, and engagement metrics.
"""

import logging
import re
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from mail_api.common.db import fetch_all, fetch_one
from mail_api.shared.domain_validation import (
    get_associated_subdomain_id,
    get_user_domain_id,
    get_user_domain_name,
)

# Import build_pagination_metadata from shared utils
from mail_api.shared.utils import build_pagination_metadata
from connect_lib.auth.hybrid import require_read_stats_hybrid

from ..utils import (
    log_operation_start,
    log_operation_success,
)

logger = logging.getLogger(__name__)

# Router for the statistics endpoints
stats_router = APIRouter(prefix="/stats", tags=["statistics"], redirect_slashes=False)


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


@stats_router.get(
    "/dashboard",
    summary="Get Dashboard Statistics",
    description="""
## Get Complete Dashboard Statistics

Retrieve comprehensive dashboard statistics for your domain.

### Metrics Included

**Summary Metrics**
- Emails sent, delivered, and delivery rates
- Open rates and click rates
- Bounce rates by type

**Engagement Analysis**
- Total opens and unique opens
- Total clicks and unique clicks
- Human-verified opens with confidence scores

**Period Comparison**
- Current vs previous period metrics
- Percentage changes
- Trend indicators

**Domain Health**
- Reputation score (excellent/good/fair/poor/critical)
- DNS verification status
- DKIM and SPF status

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `start_date` | string | 30 days ago | Start date (YYYY-MM-DD) |
| `end_date` | string | today | End date (YYYY-MM-DD) |
| `granularity` | string | day | Data granularity (day, week, month) |

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {
            "description": "Dashboard generated successfully",
            "content": {
                "application/json": {
                    "example": {
                        "status": "ok",
                        "data": {
                            "domain_name": "example.com",
                            "period": {
                                "start_date": "2025-01-01",
                                "end_date": "2025-01-31",
                                "granularity": "day",
                            },
                            "summary": {
                                "emails_sent": 10000,
                                "emails_delivered": 9500,
                                "delivery_rate": 95.0,
                                "open_rate": 25.5,
                                "click_rate": 8.2,
                                "bounce_rate": 3.5,
                            },
                            "comparison": {
                                "emails_sent": {
                                    "current": 10000,
                                    "previous": 8500,
                                    "change": "+17.6%",
                                }
                            },
                            "domain_health": {
                                "reputation_score": "excellent",
                                "dns_status": "verified",
                                "overall_status": "excellent",
                            },
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
    tags=["statistics"],
    operation_id="stats_dashboard",
)
async def get_dashboard_stats(
    start_date: str = None,
    end_date: str = None,
    granularity: str = "day",
    current_user: dict = Depends(require_read_stats_hybrid),
):
    """
    Returns the full dashboard statistics for the user's domain

    Args:
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        granularity: Data granularity (day, week, month)
    """
    try:
        # domain_name of the user's subdomain
        domain_name = get_user_subdomain_name(current_user)

        # Default period when not given (last 30 days)
        if not start_date:
            start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        if not end_date:
            end_date = datetime.now().strftime("%Y-%m-%d")

        # Convert the dates to datetime
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")

        logger.info(
            f"[DASHBOARD] Building the dashboard for domain {domain_name} from {start_date} to {end_date}"
        )

        # 1. Sent and delivered email statistics (deliveries, for the summary)
        delivery_stats = fetch_one(
            """
            SELECT
                COUNT(*) as total_emails,
                SUM(CASE WHEN status = 'delivered' THEN 1 ELSE 0 END) as delivered_emails,
                SUM(CASE WHEN status = 'bounced' THEN 1 ELSE 0 END) as bounced_emails,
                SUM(CASE WHEN status = 'deferred' THEN 1 ELSE 0 END) as deferred_emails,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_emails
            FROM deliveries
            WHERE domain_name = %s
            AND last_update >= %s AND last_update < DATE_ADD(%s, INTERVAL 1 DAY)
        """,
            (domain_name, start_date, end_date),
        )

        if not delivery_stats:
            delivery_stats = {
                "total_emails": 0,
                "delivered_emails": 0,
                "bounced_emails": 0,
                "deferred_emails": 0,
                "failed_emails": 0,
            }

        # 2. Detailed delivery event statistics (delivery_events)
        delivery_events_stats = fetch_one(
            """
            SELECT
                COUNT(*) as total_events,
                COUNT(DISTINCT message_id) as unique_messages,
                COUNT(DISTINCT rcpt) as unique_recipients,
                AVG(attempt) as avg_attempts,
                MAX(attempt) as max_attempts
            FROM delivery_events
            WHERE domain_name = %s
            AND DATE(ts) BETWEEN %s AND %s
        """,
            (domain_name, start_date, end_date),
        )

        if not delivery_events_stats:
            delivery_events_stats = {
                "total_events": 0,
                "unique_messages": 0,
                "unique_recipients": 0,
                "avg_attempts": 0,
                "max_attempts": 0,
            }

        # 3. Engagement statistics (opens and clicks)
        engagement_stats = fetch_one(
            """
            SELECT
                SUM(CASE WHEN type = 'open' THEN 1 ELSE 0 END) as total_opens,
                SUM(CASE WHEN type = 'open' AND open_status = 'confirmed_human' THEN 1 ELSE 0 END) as human_confirmed_opens,
                ROUND(AVG(CASE WHEN type = 'open' THEN open_confidence_score END)) as avg_open_score,
                SUM(CASE WHEN type = 'click' THEN 1 ELSE 0 END) as total_clicks
            FROM engagement_events ee
            JOIN deliveries d ON ee.message_id = d.message_id AND ee.rcpt = d.rcpt
            WHERE d.domain_name = %s
            AND ee.is_bot = FALSE
            AND DATE(ee.ts) BETWEEN %s AND %s
        """,
            (domain_name, start_date, end_date),
        )

        if not engagement_stats:
            engagement_stats = {"total_opens": 0, "total_clicks": 0}

        # 3. Rates
        total_emails = delivery_stats["total_emails"] or 0
        delivered_emails = delivery_stats["delivered_emails"] or 0
        total_opens = engagement_stats["total_opens"] or 0
        total_clicks = engagement_stats["total_clicks"] or 0
        human_confirmed_opens = engagement_stats.get("human_confirmed_opens", 0) or 0
        avg_open_score = engagement_stats.get("avg_open_score", 0) or 0

        # Rates (safe against division by zero)
        delivery_rate = (
            (delivered_emails / total_emails * 100) if total_emails > 0 else 0
        )
        open_rate = (
            (total_opens / delivered_emails * 100) if delivered_emails > 0 else 0
        )
        human_open_rate = (
            (human_confirmed_opens / delivered_emails * 100)
            if delivered_emails > 0
            else 0
        )
        click_rate = (
            (total_clicks / delivered_emails * 100) if delivered_emails > 0 else 0
        )
        bounce_rate = (
            (delivery_stats["bounced_emails"] / total_emails * 100)
            if total_emails > 0
            else 0
        )

        # RevBridge Connect: unsubscribes and complaints are measured.
        # Unsubscribe = engagement_events 'unsubscribe' WITHOUT the bot filter (the RFC 8058
        # one-click unsubscribe is done by the mailbox provider's server). Complaint =
        # 'complaint' suppressions (FBL/ARF) of the domain and its subdomains (the
        # Return-Path is recorded under rbm<n>.<domain>).
        unsub_row = fetch_one(
            """
            SELECT COUNT(DISTINCT ee.message_id, ee.rcpt) AS n
            FROM engagement_events ee
            JOIN deliveries d ON ee.message_id = d.message_id AND ee.rcpt = d.rcpt
            WHERE d.domain_name = %s AND ee.type = 'unsubscribe'
            AND DATE(ee.ts) BETWEEN %s AND %s
        """,
            (domain_name, start_date, end_date),
        ) or {}
        base_domain = get_user_domain_name(current_user) or domain_name
        complaint_row = fetch_one(
            """
            SELECT COUNT(*) AS n FROM suppressions
            WHERE suppression_type = 'complaint'
            AND (domain_name = %s OR domain_name LIKE %s)
            AND DATE(COALESCE(suppressed_at, created_at)) BETWEEN %s AND %s
        """,
            (base_domain, f"%.{base_domain}", start_date, end_date),
        ) or {}
        total_unsubscribes = int(unsub_row.get("n") or 0)
        total_complaints = int(complaint_row.get("n") or 0)
        unsubscribe_rate = (total_unsubscribes / delivered_emails * 100) if delivered_emails > 0 else 0
        spam_rate = (total_complaints / delivered_emails * 100) if delivered_emails > 0 else 0

        # Additional metrics
        avg_attempts = (
            delivery_events_stats.get("avg_attempts", 0) if delivery_events_stats else 0
        )
        max_attempts = (
            delivery_events_stats.get("max_attempts", 0) if delivery_events_stats else 0
        )

        # Make sure avg_attempts and max_attempts are not None
        if avg_attempts is None:
            avg_attempts = 0
        if max_attempts is None:
            max_attempts = 0

        # 4. Comparison with the previous period
        # The previous period, based on the current one
        period_days = (end_dt - start_dt).days
        previous_start = (start_dt - timedelta(days=period_days)).strftime("%Y-%m-%d")
        previous_end = start_dt.strftime("%Y-%m-%d")

        logger.info(
            f"[DASHBOARD] Computing the comparison with the previous period: {previous_start} to {previous_end}"
        )

        # Statistics of the previous period
        previous_delivery_stats = fetch_one(
            """
            SELECT
                COUNT(*) as total_emails,
                SUM(CASE WHEN status = 'delivered' THEN 1 ELSE 0 END) as delivered_emails
            FROM deliveries
            WHERE domain_name = %s
            AND DATE(last_update) BETWEEN %s AND %s
        """,
            (domain_name, previous_start, previous_end),
        )

        previous_engagement_stats = fetch_one(
            """
            SELECT
                SUM(CASE WHEN type = 'open' THEN 1 ELSE 0 END) as total_opens,
                SUM(CASE WHEN type = 'open' AND open_status = 'confirmed_human' THEN 1 ELSE 0 END) as human_confirmed_opens,
                ROUND(AVG(CASE WHEN type = 'open' THEN open_confidence_score END)) as avg_open_score,
                SUM(CASE WHEN type = 'click' THEN 1 ELSE 0 END) as total_clicks
            FROM engagement_events ee
            JOIN deliveries d ON ee.message_id = d.message_id AND ee.rcpt = d.rcpt
            WHERE d.domain_name = %s
            AND ee.is_bot = FALSE
            AND DATE(ee.ts) BETWEEN %s AND %s
        """,
            (domain_name, previous_start, previous_end),
        )

        # Default values for the previous period
        if not previous_delivery_stats:
            previous_delivery_stats = {"total_emails": 0, "delivered_emails": 0}
        if not previous_engagement_stats:
            previous_engagement_stats = {"total_opens": 0, "total_clicks": 0}

        # Metrics of the previous period
        prev_total_emails = previous_delivery_stats["total_emails"] or 0
        prev_delivered_emails = previous_delivery_stats["delivered_emails"] or 0
        prev_total_opens = previous_engagement_stats["total_opens"] or 0
        prev_total_clicks = previous_engagement_stats["total_clicks"] or 0
        prev_human_confirmed_opens = (
            previous_engagement_stats.get("human_confirmed_opens", 0) or 0
        )
        prev_avg_open_score = previous_engagement_stats.get("avg_open_score", 0) or 0

        # Rates of the previous period
        prev_delivery_rate = (
            (prev_delivered_emails / prev_total_emails * 100)
            if prev_total_emails > 0
            else 0
        )
        prev_open_rate = (
            (prev_total_opens / prev_delivered_emails * 100)
            if prev_delivered_emails > 0
            else 0
        )
        prev_human_open_rate = (
            (prev_human_confirmed_opens / prev_delivered_emails * 100)
            if prev_delivered_emails > 0
            else 0
        )
        prev_click_rate = (
            (prev_total_clicks / prev_delivered_emails * 100)
            if prev_delivered_emails > 0
            else 0
        )

        # Percentage changes
        def calculate_change(current, previous):
            if previous == 0:
                return "+100%" if current > 0 else "0%"
            change = ((current - previous) / previous) * 100
            if change > 0:
                return f"+{change:.1f}%"
            elif change < 0:
                return f"{change:.1f}%"
            else:
                return "0%"

        # Apply the calculation
        emails_sent_change = calculate_change(total_emails, prev_total_emails)
        delivery_rate_change = calculate_change(delivery_rate, prev_delivery_rate)
        open_rate_change = calculate_change(open_rate, prev_open_rate)
        human_open_rate_change = calculate_change(human_open_rate, prev_human_open_rate)
        open_avg_score_change = calculate_change(avg_open_score, prev_avg_open_score)
        click_rate_change = calculate_change(click_rate, prev_click_rate)

        # Comparison object
        comparison = {
            "period": {
                "current": {"start": start_date, "end": end_date},
                "previous": {"start": previous_start, "end": previous_end},
            },
            "emails_sent": {
                "current": total_emails,
                "previous": prev_total_emails,
                "change": emails_sent_change,
            },
            "delivery_rate": {
                "current": round(delivery_rate, 1),
                "previous": round(prev_delivery_rate, 1),
                "change": delivery_rate_change,
            },
            "open_rate": {
                "current": round(open_rate, 1),
                "previous": round(prev_open_rate, 1),
                "change": open_rate_change,
            },
            "human_open_rate": {
                "current": round(human_open_rate, 1),
                "previous": round(prev_human_open_rate, 1),
                "change": human_open_rate_change,
            },
            "open_avg_confidence_score": {
                "current": int(avg_open_score),
                "previous": int(prev_avg_open_score),
                "change": open_avg_score_change,
            },
            "click_rate": {
                "current": round(click_rate, 1),
                "previous": round(prev_click_rate, 1),
                "change": click_rate_change,
            },
        }

        logger.info(
            f"[DASHBOARD] Comparison computed: emails {emails_sent_change}, delivery {delivery_rate_change}, open {open_rate_change}, click {click_rate_change}"
        )

        # 5. Domain health (real checks)
        domain_health = await check_domain_health(domain_name)

        # 6. Build the response
        dashboard_data = {
            "domain_name": domain_name,
            "period": {
                "start_date": start_date,
                "end_date": end_date,
                "granularity": granularity,
            },
            "summary": {
                "emails_sent": total_emails,
                "emails_delivered": delivered_emails,
                "delivery_rate": round(delivery_rate, 1),
                "open_rate": round(open_rate, 1),
                "human_open_rate": round(human_open_rate, 1),
                "click_rate": round(click_rate, 1),
                "bounce_rate": round(bounce_rate, 1),
                "open_avg_confidence_score": int(avg_open_score),
                "spam_rate": round(spam_rate, 2),
                "unsubscribe_rate": round(unsubscribe_rate, 2),
                "unsubscribes": total_unsubscribes,
                "complaints": total_complaints,
            },
            "delivery_metrics": {
                "total_events": delivery_events_stats.get("total_events", 0),
                "unique_messages": delivery_events_stats.get("unique_messages", 0),
                "unique_recipients": delivery_events_stats.get("unique_recipients", 0),
                "average_attempts": round(avg_attempts, 1),
                "max_attempts": max_attempts,
            },
            "comparison": comparison,
            "domain_health": domain_health,
        }

        logger.info(
            f"[DASHBOARD] Dashboard built for domain {domain_name}"
        )

        return {
            "status": "ok",
            "data": dashboard_data,
            "timestamp": datetime.now().isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[DASHBOARD] Error building the dashboard: {e}")
        raise HTTPException(
            status_code=500, detail="Error building the dashboard statistics"
        ) from e


# Domain health helpers
async def check_domain_health(domain_name: str) -> dict:
    """
    Checks the real health of a domain from delivery and engagement metrics

    Args:
        domain_name: Domain name to check

    Returns:
        dict: Domain health status
    """
    try:
        logger.info(
            f"[DOMAIN_HEALTH] Starting the health check of domain {domain_name}"
        )

        # 1. Reputation score from real metrics
        reputation_score = await calculate_reputation_score(domain_name)

        # 2. DNS status from successful deliveries
        dns_status = await check_dns_status(domain_name)

        # 3. DKIM status from delivery rates
        dkim_status = await check_dkim_status(domain_name)

        # 4. SPF status from bounce rates
        spf_status = await check_spf_status(domain_name)

        # 5. Overall domain status
        overall_status = await calculate_overall_status(
            reputation_score, dns_status, dkim_status, spf_status
        )

        domain_health = {
            "reputation_score": reputation_score,
            "dns_status": dns_status,
            "dkim_status": dkim_status,
            "spf_status": spf_status,
            "overall_status": overall_status,
            "last_check": datetime.now().isoformat(),
        }

        logger.info(
            f"[DOMAIN_HEALTH] Check done: {overall_status} (score: {reputation_score})"
        )
        return domain_health

    except Exception as e:
        logger.error(
            f"[DOMAIN_HEALTH] Error checking the health of domain {domain_name}: {e}"
        )
        return {
            "reputation_score": "unknown",
            "dns_status": "unknown",
            "dkim_status": "unknown",
            "spf_status": "unknown",
            "overall_status": "error",
            "last_check": datetime.now().isoformat(),
            "error": str(e),
        }


async def calculate_reputation_score(domain_name: str) -> str:
    """Computes the reputation score from real metrics"""
    try:
        # Metrics of the last 30 days
        end_date = datetime.now()
        start_date = end_date - timedelta(days=30)

        # Delivery statistics
        delivery_stats = fetch_one(
            """
            SELECT
                COUNT(*) as total_emails,
                SUM(CASE WHEN status = 'delivered' THEN 1 ELSE 0 END) as delivered_emails,
                SUM(CASE WHEN status = 'bounced' THEN 1 ELSE 0 END) as bounced_emails,
                SUM(CASE WHEN status = 'deferred' THEN 1 ELSE 0 END) as deferred_emails
            FROM deliveries
            WHERE domain_name = %s
            AND DATE(last_update) BETWEEN %s AND %s
        """,
            (domain_name, start_date, end_date),
        )

        if not delivery_stats or delivery_stats["total_emails"] == 0:
            return "unknown"

        total_emails = delivery_stats["total_emails"]
        delivered_emails = delivery_stats["delivered_emails"] or 0
        bounced_emails = delivery_stats["bounced_emails"] or 0
        deferred_emails = delivery_stats["deferred_emails"] or 0

        # Score from the rates
        delivery_rate = (delivered_emails / total_emails) * 100
        bounce_rate = (bounced_emails / total_emails) * 100
        deferred_rate = (deferred_emails / total_emails) * 100

        # Score from the reputation criteria
        if delivery_rate >= 95 and bounce_rate <= 2 and deferred_rate <= 5:
            return "excellent"
        elif delivery_rate >= 90 and bounce_rate <= 5 and deferred_rate <= 10:
            return "good"
        elif delivery_rate >= 80 and bounce_rate <= 10 and deferred_rate <= 15:
            return "fair"
        elif delivery_rate >= 70 and bounce_rate <= 15 and deferred_rate <= 20:
            return "poor"
        else:
            return "critical"

    except Exception as e:
        logger.error(f"[DOMAIN_HEALTH] Error computing the reputation score: {e}")
        return "unknown"


async def check_dns_status(domain_name: str) -> str:
    """Checks the DNS status from successful deliveries"""
    try:
        # Are there recent successful deliveries (last 7 days)?
        end_date = datetime.now()
        start_date = end_date - timedelta(days=7)

        result = fetch_one(
            """
            SELECT COUNT(*) as successful_deliveries
            FROM deliveries
            WHERE domain_name = %s
            AND status = 'delivered'
            AND DATE(last_update) BETWEEN %s AND %s
        """,
            (domain_name, start_date, end_date),
        )

        if result and result["successful_deliveries"] > 0:
            return "verified"
        else:
            return "unverified"

    except Exception as e:
        logger.error(f"[DOMAIN_HEALTH] Error checking the DNS status: {e}")
        return "unknown"


async def check_dkim_status(domain_name: str) -> str:
    """Checks the DKIM status from delivery rates"""
    try:
        # Delivery rate of the last 7 days
        end_date = datetime.now()
        start_date = end_date - timedelta(days=7)

        result = fetch_one(
            """
            SELECT
                COUNT(*) as total_emails,
                SUM(CASE WHEN status = 'delivered' THEN 1 ELSE 0 END) as delivered_emails
            FROM deliveries
            WHERE domain_name = %s
            AND DATE(last_update) BETWEEN %s AND %s
        """,
            (domain_name, start_date, end_date),
        )

        if result and result["total_emails"] > 0:
            delivery_rate = (result["delivered_emails"] / result["total_emails"]) * 100
            # DKIM directly affects the delivery rate
            if delivery_rate >= 90:
                return "verified"
            elif delivery_rate >= 70:
                return "partial"
            else:
                return "unverified"
        else:
            return "unknown"

    except Exception as e:
        logger.error(f"[DOMAIN_HEALTH] Error checking the DKIM status: {e}")
        return "unknown"


async def check_spf_status(domain_name: str) -> str:
    """Checks the SPF status from bounce rates"""
    try:
        # Bounce rate of the last 7 days
        end_date = datetime.now()
        start_date = end_date - timedelta(days=7)

        result = fetch_one(
            """
            SELECT
                COUNT(*) as total_emails,
                SUM(CASE WHEN status = 'bounced' THEN 1 ELSE 0 END) as bounced_emails
            FROM deliveries
            WHERE domain_name = %s
            AND DATE(last_update) BETWEEN %s AND %s
        """,
            (domain_name, start_date, end_date),
        )

        if result and result["total_emails"] > 0:
            bounce_rate = (result["bounced_emails"] / result["total_emails"]) * 100
            # SPF directly affects the bounce rate
            if bounce_rate <= 2:
                return "verified"
            elif bounce_rate <= 5:
                return "partial"
            else:
                return "unverified"
        else:
            return "unknown"

    except Exception as e:
        logger.error(f"[DOMAIN_HEALTH] Error checking the SPF status: {e}")
        return "unknown"


async def calculate_overall_status(
    reputation_score: str, dns_status: str, dkim_status: str, spf_status: str
) -> str:
    """Computes the overall status from every indicator"""
    try:
        # Map the scores to numbers
        score_map = {
            "excellent": 5,
            "good": 4,
            "fair": 3,
            "poor": 2,
            "critical": 1,
            "unknown": 0,
        }

        status_map = {"verified": 3, "partial": 2, "unverified": 1, "unknown": 0}

        # Average score
        reputation_value = score_map.get(reputation_score, 0)
        dns_value = status_map.get(dns_status, 0)
        dkim_value = status_map.get(dkim_status, 0)
        spf_value = status_map.get(spf_status, 0)

        # Weights: reputation (40%), DNS (20%), DKIM (20%), SPF (20%)
        weighted_score = (
            reputation_value * 0.4
            + dns_value * 0.2
            + dkim_value * 0.2
            + spf_value * 0.2
        )

        # Map the score to a status
        if weighted_score >= 4.0:
            return "excellent"
        elif weighted_score >= 3.0:
            return "good"
        elif weighted_score >= 2.0:
            return "fair"
        elif weighted_score >= 1.0:
            return "poor"
        else:
            return "critical"

    except Exception as e:
        logger.error(f"[DOMAIN_HEALTH] Error computing the overall status: {e}")
        return "unknown"


@stats_router.get(
    "/timeseries",
    summary="Get Time Series Statistics",
    description="""
## Get Time Series Statistics

Retrieve time series statistics for trend analysis and reporting.

### Data Returned

Each data point includes:
- `sent`: Number of emails sent
- `delivered`: Number of emails delivered
- `opened`: Number of opens
- `clicked`: Number of clicks
- `bounced`: Number of bounces
- `delivery_rate`: Delivery rate percentage
- `open_rate`: Open rate percentage
- `click_rate`: Click rate percentage

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `start_date` | string | 30 days ago | Start date (YYYY-MM-DD) |
| `end_date` | string | today | End date (YYYY-MM-DD) |
| `granularity` | string | day | Data granularity |

### Granularity Options

- `day`: Daily data points (default)
- `week`: Weekly data points (ISO week)
- `month`: Monthly data points

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {
            "description": "Time series generated successfully",
            "content": {
                "application/json": {
                    "example": {
                        "status": "ok",
                        "domain_name": "example.com",
                        "period": {
                            "start_date": "2025-01-01",
                            "end_date": "2025-01-31",
                            "granularity": "day",
                        },
                        "data": [
                            {
                                "date": "2025-01-01",
                                "sent": 1000,
                                "delivered": 950,
                                "opened": 240,
                                "clicked": 80,
                                "delivery_rate": 95.0,
                                "open_rate": 25.3,
                                "click_rate": 8.4,
                            }
                        ],
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
    tags=["statistics"],
    operation_id="stats_timeseries",
)
async def get_timeseries_stats(
    start_date: str = Query(
        None, description="Start date (YYYY-MM-DD)", examples=["2025-01-01"]
    ),
    end_date: str = Query(
        None, description="End date (YYYY-MM-DD)", examples=["2025-01-31"]
    ),
    granularity: str = Query(
        "day", description="Data granularity (day, week, month)", examples=["day"]
    ),
    current_user: dict = Depends(require_read_stats_hybrid),
):
    """
    Returns time series statistics for the user's domain

    Args:
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        granularity: Data granularity (day, week, month)
    """
    try:
        # domain_name of the user's subdomain
        domain_name = get_user_subdomain_name(current_user)

        # Default period when not given (last 30 days)
        if not start_date:
            start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        if not end_date:
            end_date = datetime.now().strftime("%Y-%m-%d")

        # Convert the dates to datetime
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")

        logger.info(
            f"[TIMESERIES] Building the time series for domain {domain_name} from {start_date} to {end_date} (granularity: {granularity})"
        )

        # Date format for the granularity
        if granularity == "day":
            date_format = "%Y-%m-%d"
            group_by = "DATE(ts)"
        elif granularity == "week":
            date_format = "%Y-%u"  # Year + ISO week
            group_by = "YEARWEEK(ts, 1)"
        elif granularity == "month":
            date_format = "%Y-%m"
            group_by = "DATE_FORMAT(ts, '%Y-%m')"
        else:
            # Fall back to day
            date_format = "%Y-%m-%d"
            group_by = "DATE(ts)"
            granularity = "day"

        # Delivery data per period
        # Same logic as the dashboard: count unique rows of the deliveries table
        if granularity == "day":
            d_group_by_expr = "DATE_FORMAT(d.last_update, '%Y-%m-%d')"  # <-- string
        elif granularity == "week":
            d_group_by_expr = "DATE_FORMAT(d.last_update, '%x-%v')"  # ISO: YYYY-WW
        elif granularity == "month":
            d_group_by_expr = "DATE_FORMAT(d.last_update, '%Y-%m')"
        else:
            granularity = "day"
            d_group_by_expr = "DATE_FORMAT(d.last_update, '%Y-%m-%d')"

        delivery_data = fetch_all(
            f"""
            SELECT
                {d_group_by_expr} as period,
                COUNT(*) as sent,
                SUM(CASE WHEN d.status = 'delivered' THEN 1 ELSE 0 END) as delivered,
                SUM(CASE WHEN d.status = 'bounced' THEN 1 ELSE 0 END) as bounced,
                SUM(CASE WHEN d.status = 'deferred' THEN 1 ELSE 0 END) as deferred,
                SUM(CASE WHEN d.status = 'failed' THEN 1 ELSE 0 END) as failed
            FROM deliveries d
            WHERE d.domain_name = %s
            AND DATE(d.last_update) BETWEEN %s AND %s
            GROUP BY {d_group_by_expr}
            ORDER BY period
        """,
            (domain_name, start_date, end_date),
        )

        # Engagement data per period
        if granularity == "day":
            ee_group_by_expr = "DATE_FORMAT(ee.ts, '%Y-%m-%d')"  # <-- string
        elif granularity == "week":
            ee_group_by_expr = "DATE_FORMAT(ee.ts, '%x-%v')"
        elif granularity == "month":
            ee_group_by_expr = "DATE_FORMAT(ee.ts, '%Y-%m')"
        else:
            ee_group_by_expr = "DATE_FORMAT(ee.ts, '%Y-%m-%d')"

        engagement_data = fetch_all(
            f"""
            SELECT
                {ee_group_by_expr} as period,
                SUM(CASE WHEN ee.type = 'open' THEN 1 ELSE 0 END) as opened,
                SUM(CASE WHEN ee.type = 'open' AND ee.open_status = 'confirmed_human' THEN 1 ELSE 0 END) as opened_human,
                ROUND(AVG(CASE WHEN ee.type = 'open' THEN ee.open_confidence_score END)) as avg_open_score,
                SUM(CASE WHEN ee.type = 'click' THEN 1 ELSE 0 END) as clicked
            FROM engagement_events ee
            JOIN deliveries d ON ee.message_id = d.message_id AND ee.rcpt = d.rcpt
            WHERE d.domain_name = %s
            AND ee.is_bot = FALSE
            AND DATE(ee.ts) BETWEEN %s AND %s
            GROUP BY {ee_group_by_expr}
            ORDER BY period
        """,
            (domain_name, start_date, end_date),
        )

        # Unsubscribe and spam complaint series: placeholder queries (they return 1 per
        # period with deliveries, not real events).
        # Note: the existing suppressions table could provide the real numbers
        unsubscribe_data = fetch_all(
            f"""
            SELECT
                period,
                COUNT(*) as unsubscribed
            FROM (
                SELECT {d_group_by_expr} as period, 1 as dummy
                FROM deliveries d
                WHERE d.domain_name = %s
                AND DATE(d.last_update) BETWEEN %s AND %s
                GROUP BY {d_group_by_expr}
            ) as dummy_data
            GROUP BY period
            ORDER BY period
        """,
            (domain_name, start_date, end_date),
        )

        spam_data = fetch_all(
            f"""
            SELECT
                period,
                COUNT(*) as spam_complaints
            FROM (
                SELECT {d_group_by_expr} as period, 1 as dummy
                FROM deliveries d
                WHERE d.domain_name = %s
                AND DATE(d.last_update) BETWEEN %s AND %s
                GROUP BY {d_group_by_expr}
            ) as dummy_data
            GROUP BY period
            ORDER BY period
        """,
            (domain_name, start_date, end_date),
        )

        # Convert to dictionaries for easy lookup
        delivery_dict = {row["period"]: row for row in delivery_data}
        engagement_dict = {row["period"]: row for row in engagement_data}
        unsubscribe_dict = {row["period"]: row for row in unsubscribe_data}
        spam_dict = {row["period"]: row for row in spam_data}

        # List of periods
        periods = []
        current_date = start_dt

        if granularity == "day":
            while current_date <= end_dt:
                periods.append(current_date.strftime("%Y-%m-%d"))
                current_date += timedelta(days=1)

        elif granularity == "week":
            # ISO week "YYYY-WW", to match MySQL's '%x-%v'
            while current_date <= end_dt:
                iso = current_date.isocalendar()  # (year, week, weekday)
                periods.append(f"{iso.year}-{iso.week:02d}")
                current_date += timedelta(weeks=1)

        elif granularity == "month":
            while current_date <= end_dt:
                periods.append(current_date.strftime("%Y-%m"))
                # move forward 1 month
                year = current_date.year + (current_date.month // 12)
                month = 1 if current_date.month == 12 else current_date.month + 1
                current_date = current_date.replace(year=year, month=month)

        # Build the time series data
        timeseries_data = []
        for period in periods:
            # Delivery data
            delivery_row = delivery_dict.get(period, {})
            sent = delivery_row.get("sent", 0)
            delivered = delivery_row.get("delivered", 0)
            bounced = delivery_row.get("bounced", 0)
            deferred = delivery_row.get("deferred", 0)
            failed = delivery_row.get("failed", 0)

            # Engagement data
            engagement_row = engagement_dict.get(period, {})
            opened = engagement_row.get("opened", 0)
            opened_human = engagement_row.get("opened_human", 0)
            clicked = engagement_row.get("clicked", 0)
            avg_open_score = engagement_row.get("avg_open_score", 0)

            # Unsubscribe and spam data (placeholders, see above)
            unsubscribe_row = unsubscribe_dict.get(period, {})
            unsubscribed = unsubscribe_row.get("unsubscribed", 0)

            spam_row = spam_dict.get(period, {})
            spam_complaints = spam_row.get("spam_complaints", 0)

            # Rates (safe against division by zero)
            delivery_rate = (delivered / sent * 100) if sent > 0 else 0
            open_rate = (opened / delivered * 100) if delivered > 0 else 0
            human_open_rate = (opened_human / delivered * 100) if delivered > 0 else 0
            click_rate = (clicked / delivered * 100) if delivered > 0 else 0
            bounce_rate = (bounced / sent * 100) if sent > 0 else 0

            # Date shown to the user
            if granularity == "day":
                display_date = period
            elif granularity == "week":
                # ISO week (the start date of the week could be computed here)
                year, _week = period.split("-")
                # Note: the ISO week is returned as is
                display_date = period
            else:  # month
                display_date = period

            timeseries_data.append(
                {
                    "date": display_date,
                    "sent": sent,
                    "delivered": delivered,
                    "opened": opened,
                    "clicked": clicked,
                    "opened_human": opened_human,
                    "bounced": bounced,
                    "deferred": deferred,
                    "failed": failed,
                    "unsubscribed": unsubscribed,
                    "spam_complaints": spam_complaints,
                    "delivery_rate": round(delivery_rate, 1),
                    "open_rate": round(open_rate, 1),
                    "human_open_rate": round(human_open_rate, 1),
                    "open_avg_confidence_score": int(avg_open_score or 0),
                    "click_rate": round(click_rate, 1),
                    "bounce_rate": round(bounce_rate, 1),
                }
            )

        logger.info(
            f"[TIMESERIES] Time series built: {len(timeseries_data)} periods"
        )

        return {
            "status": "ok",
            "domain_name": domain_name,
            "period": {
                "start_date": start_date,
                "end_date": end_date,
                "granularity": granularity,
            },
            "data": timeseries_data,
            "timestamp": datetime.now().isoformat(),
        }

    except Exception as e:
        logger.error(f"[TIMESERIES] Error building the time series: {e}")
        raise HTTPException(
            status_code=500, detail="Error building the time series statistics"
        ) from e


@stats_router.get(
    "/activity",
    summary="Get Activity Log",
    description="""
## Get Detailed Activity Log

Retrieve detailed activity logs with advanced filtering and pagination.

### Event Types

**Delivery Events**
- `delivered`: Email successfully delivered
- `bounced`: Email bounced
- `deferred`: Delivery deferred (temporary failure)
- `failed`: Delivery failed permanently

**Engagement Events**
- `opened`: Email opened
- `clicked`: Link clicked
- `unsubscribed`: Recipient unsubscribed

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `page` | integer | 1 | Page number |
| `limit` | integer | 50 | Items per page (max: 100) |
| `event_type` | string | - | Filter by event type |
| `start_date` | string | - | Start date (YYYY-MM-DD) |
| `end_date` | string | - | End date (YYYY-MM-DD) |
| `status` | string | - | Filter by status (success, error, pending) |

### Response Fields

Each activity includes:
- Event details (type, timestamp, message_id)
- Recipient and sender information
- Status and metadata (IP, user agent, attempts)

### Authentication

Requires a valid JWT token or API key in the `Authorization` header.
    """,
    responses={
        200: {
            "description": "Activity log retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "status": "ok",
                        "domain_name": "example.com",
                        "pagination": {
                            "page": 1,
                            "limit": 50,
                            "total": 150,
                            "total_pages": 3,
                            "has_next": True,
                            "has_prev": False,
                        },
                        "activities": [
                            {
                                "id": 12345,
                                "timestamp": "2025-01-21T16:30:00Z",
                                "event_type": "delivered",
                                "message_id": "msg_1234567890abcdef",
                                "recipient": {
                                    "email": "user@example.com",
                                    "name": "John Doe",
                                },
                                "sender": {
                                    "email": "noreply@example.com",
                                    "name": "Example Corp",
                                },
                                "subject": "Welcome!",
                                "status": "success",
                                "metadata": {
                                    "ip_address": "203.0.113.10",
                                    "attempt": 1,
                                    "host": "smtp.example.com",
                                },
                            }
                        ],
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
    tags=["statistics"],
    operation_id="stats_activity",
)
async def get_activity_stats(
    page: int = Query(1, ge=1, description="Page number", examples=[1]),
    limit: int = Query(
        50, ge=1, le=100, description="Items per page", examples=[50]
    ),
    event_type: str = Query(
        None, description="Filter by event type", examples=["delivered"]
    ),
    start_date: str = Query(
        None, description="Start date (YYYY-MM-DD)", examples=["2025-01-01"]
    ),
    end_date: str = Query(
        None, description="End date (YYYY-MM-DD)", examples=["2025-01-31"]
    ),
    status: str = Query(None, description="Filter by status", examples=["success"]),
    current_user: dict = Depends(require_read_stats_hybrid),
):
    # domain_name of the user's subdomain
    domain_name = get_user_subdomain_name(current_user)
    """
    Returns the detailed activity of a domain, with pagination

    Args:
        domain_name: Domain name
        page: Page number (default: 1)
        limit: Items per page (default: 50, maximum: 100)
        event_type: Filter by event type (delivered, bounced, opened, clicked)
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        status: Filter by status (success, error, pending)
    """
    try:
        # Validate and compute the pagination
        if page < 1:
            page = 1
        if limit < 1:
            limit = 10
        if limit > 100:
            limit = 100
        offset = (page - 1) * limit

        # Classify event_type: engagement events live in engagement_events table
        # with DB values "open"/"click", while delivery events live in delivery_events
        # with DB values "delivered"/"bounced"/"deferred"/"failed"/"sent".
        engagement_type_map = {"opened": "open", "clicked": "click", "unsubscribed": "unsubscribe"}
        is_engagement_filter = event_type in engagement_type_map
        is_delivery_filter = event_type and not is_engagement_filter

        # Build the WHERE filters (delivery_events side)
        where_conditions = ["d.domain_name = %s"]
        params = [domain_name]

        if is_delivery_filter:
            where_conditions.append("de.status = %s")
            params.append(event_type)

        if start_date:
            where_conditions.append("DATE(de.ts) >= %s")
            params.append(start_date)

        if end_date:
            where_conditions.append("DATE(de.ts) <= %s")
            params.append(end_date)

        if status:
            # Map the status to the real database values (delivery_events.status)
            status_mapping = {
                "success": ["delivered"],
                "error": ["bounced", "failed"],
                "pending": ["deferred", "sent"],
            }
            if status in status_mapping:
                status_values = status_mapping[status]
                placeholders = ", ".join(["%s"] * len(status_values))
                where_conditions.append(f"de.status IN ({placeholders})")
                params.extend(status_values)

        where_clause = " AND ".join(where_conditions)

        log_operation_start("ACTIVITY", domain_name=domain_name, page=page, limit=limit)

        # Total rows for the pagination - counting both event types
        count_query = f"""
            SELECT (
                (
                    SELECT COUNT(DISTINCT de.id) as total
                    FROM delivery_events de
                    JOIN deliveries d ON de.message_id = d.message_id AND de.rcpt = d.rcpt
                    WHERE {where_clause}
                    {"AND 1=0" if is_engagement_filter else ""}
                ) + (
                    SELECT COUNT(DISTINCT ee.id) as total
                    FROM engagement_events ee
                    JOIN deliveries d ON ee.message_id = d.message_id AND ee.rcpt = d.rcpt
                    WHERE d.domain_name = %s
                    {"AND DATE(ee.ts) >= %s" if start_date else ""}
                    {"AND DATE(ee.ts) <= %s" if end_date else ""}
                    {"AND ee.type = %s" if is_engagement_filter else ""}
                    {"AND 1=0" if is_delivery_filter else ""}
                    {"AND ee.type IN ('open', 'click')" if status == 'success' and not is_engagement_filter else ""}
                    {"AND 1=0" if status in ('error', 'pending') else ""}
                )
            ) as total
        """

        # Parameters of the count query
        count_engagement_params = [domain_name]
        if start_date:
            count_engagement_params.append(start_date)
        if end_date:
            count_engagement_params.append(end_date)
        if is_engagement_filter:
            count_engagement_params.append(engagement_type_map[event_type])

        count_params = params + count_engagement_params
        total_result = fetch_one(count_query, count_params)
        total = total_result["total"] if total_result else 0

        # Pagination metadata from the utilities
        pagination_metadata = build_pagination_metadata(page, limit, total)

        # Fetch the activity with pagination - UNION combines delivery and engagement events
        # (no join with webhook_events, so one event never appears more than once)
        activities_query = f"""
            (
                SELECT
                    de.id,
                    de.ts as timestamp,
                    de.status as event_type,
                    de.message_id,
                    de.rcpt,
                    de.domain_name,
                    de.queue_id,
                    de.dsn,
                    de.attempt,
                    de.host,
                    de.service,
                    NULL as engagement_type,
                    NULL as click_url,
                    NULL as ip_address,
                    NULL as user_agent,
                    NULL as open_source,
                    NULL as open_status,
                    NULL as open_confidence_score,
                    'delivery' as event_source,
                    NULL as sender_email,
                    NULL as sender_name,
                    NULL as subject
                FROM delivery_events de
                JOIN deliveries d ON de.message_id = d.message_id AND de.rcpt = d.rcpt
                WHERE {where_clause}
                {"AND 1=0" if is_engagement_filter else ""}
            )
            UNION ALL
            (
                SELECT
                    ee.id,
                    ee.ts as timestamp,
                    ee.type as event_type,
                    ee.message_id,
                    ee.rcpt,
                    d.domain_name,
                    NULL as queue_id,
                    NULL as dsn,
                    NULL as attempt,
                    NULL as host,
                    NULL as service,
                    ee.type as engagement_type,
                    ee.url as click_url,
                    ee.ip_address,
                    ee.user_agent,
                    ee.open_source,
                    ee.open_status,
                    ee.open_confidence_score,
                    'engagement' as event_source,
                    NULL as sender_email,
                    NULL as sender_name,
                    NULL as subject
                FROM engagement_events ee
                JOIN deliveries d ON ee.message_id = d.message_id AND ee.rcpt = d.rcpt
                WHERE d.domain_name = %s
                {"AND DATE(ee.ts) >= %s" if start_date else ""}
                {"AND DATE(ee.ts) <= %s" if end_date else ""}
                {"AND ee.type = %s" if is_engagement_filter else ""}
                {"AND 1=0" if is_delivery_filter else ""}
                {"AND ee.type IN ('open', 'click')" if status == 'success' and not is_engagement_filter else ""}
                {"AND 1=0" if status in ('error', 'pending') else ""}
            )
            ORDER BY timestamp DESC
            LIMIT %s OFFSET %s
        """

        # Parameters of the UNION query (engagement_events side)
        engagement_params = [domain_name]
        if start_date:
            engagement_params.append(start_date)
        if end_date:
            engagement_params.append(end_date)
        if is_engagement_filter:
            engagement_params.append(engagement_type_map[event_type])

        # Combine the parameters: delivery_events + engagement_events + pagination
        query_params = params + engagement_params + [limit, offset]

        activities_result = fetch_all(activities_query, query_params)

        # Process and format the activity
        activities = []
        for row in activities_result:
            # Event type and status from the event source
            if row["event_source"] == "engagement":
                # Engagement events (open, click, unsubscribe)
                if row["engagement_type"] == "open":
                    event_type = "opened"
                    status = "success"
                elif row["engagement_type"] == "click":
                    event_type = "clicked"
                    status = "success"
                elif row["engagement_type"] == "unsubscribe":
                    event_type = "unsubscribed"
                    status = "success"
                else:
                    event_type = row["event_type"]
                    status = "success"
            else:
                # Delivery events
                event_type = row["event_type"]
                # Map the delivery status to the general status
                if row["event_type"] in ["delivered"]:
                    status = "success"
                elif row["event_type"] in ["bounced", "failed"]:
                    status = "error"
                else:
                    status = "pending"

            # Build the metadata
            metadata = {
                "ip_address": row.get("ip_address"),
                "user_agent": row.get("user_agent"),
                "bounce_reason": (
                    row.get("dsn") if row["event_type"] == "bounced" else None
                ),
                "click_url": (
                    row.get("click_url") if row["engagement_type"] == "click" else None
                ),
                "queue_id": row.get("queue_id"),
                "attempt": row.get("attempt"),
                "host": row.get("host"),
                "service": row.get("service"),
            }

            # Drop None values
            metadata = {k: v for k, v in metadata.items() if v is not None}
            # Expose the score when available
            if row["event_source"] == "engagement" and row["engagement_type"] == "open":
                if row.get("open_confidence_score") is not None:
                    metadata["open_confidence_score"] = int(
                        row.get("open_confidence_score")
                    )

            # Include open_source/open_status for open events
            if row["event_source"] == "engagement" and row["engagement_type"] == "open":
                if row.get("open_source") is not None:
                    metadata["open_source"] = row.get("open_source")
                if row.get("open_status") is not None:
                    metadata["open_status"] = row.get("open_status")

            # Build the activity
            activity = {
                "id": row["id"],
                "timestamp": row["timestamp"].isoformat() if row["timestamp"] else None,
                "event_type": event_type,
                "message_id": row["message_id"],
                "recipient": {
                    "email": row["rcpt"],
                    "name": None,  # The recipient's name is not stored
                },
                "sender": {
                    "email": row.get("sender_email", f"noreply@{row['domain_name']}"),
                    "name": row.get(
                        "sender_name",
                        (
                            row["domain_name"].split(".")[0].title()
                            if row["domain_name"]
                            else None
                        ),
                    ),
                },
                "subject": row.get("subject"),
                "status": status,
                "metadata": metadata,
            }

            activities.append(activity)

        log_operation_success("ACTIVITY", activities_found=len(activities), total=total)

        return {
            "status": "ok",
            "domain_name": domain_name,
            "pagination": pagination_metadata,
            "activities": activities,
            "timestamp": datetime.now().isoformat(),
        }

    except Exception as e:
        logger.error(f"[ACTIVITY] Error fetching the activity: {e}")
        raise HTTPException(status_code=500, detail="Error fetching the activity") from e


@stats_router.get(
    "/message/headers",
    summary="Get Email Headers",
    description="Retrieve the stored email headers (subject, sender, recipients, Message-ID) for a specific message.",
    tags=["statistics"],
    operation_id="stats_message_headers",
)
async def get_message_headers(
    message_id: str = Query(..., description="Message ID"),
    rcpt: str | None = Query(
        None,
        description="Recipient of one copy: `to` then shows only that address. "
        "Without it, `to` lists every recipient of the send.",
    ),
    current_user: dict = Depends(require_read_stats_hybrid),
):
    """
    Returns the headers that Connect stores (message_meta + send_ledger) for the message.
    """
    from .stats_mysql import message_headers_from_mysql

    # Normalize message_id: accept it with or without angle brackets
    clean_id = message_id.strip().strip("<>").strip()

    result = await message_headers_from_mysql(current_user, clean_id.split("@", 1)[0], rcpt)
    if not result:
        raise HTTPException(status_code=404, detail="Headers not found for this message")
    return result


@stats_router.get(
    "/messages",
    summary="Get Messages (grouped by message_id)",
    description="Retrieve messages with their event timeline, grouped by message_id.",
    tags=["statistics"],
    operation_id="stats_messages",
)
async def get_messages(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    search: str | None = Query(None, description="Search by recipient email or subject"),
    event_type: str | None = Query(None, description="Filter messages that have this event type"),
    status: str | None = Query(None, description="Filter by delivery status (success, error, pending)"),
    start_date: str | None = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: str | None = Query(None, description="End date (YYYY-MM-DD)"),
    current_user: dict = Depends(require_read_stats_hybrid),
):
    """
    Returns messages grouped by message_id, with their event timeline.
    Source: MySQL (send_ledger, delivery_events, engagement_events, message_meta).
    """
    from .stats_mysql import messages_from_mysql

    domain_name = get_user_subdomain_name(current_user)
    return await messages_from_mysql(
        current_user, domain_name, page, limit, start_date, end_date, status, event_type, search
    )
