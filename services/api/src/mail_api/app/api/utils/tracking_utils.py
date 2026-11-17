"""
Tracking data processing utilities

Functions that process the data collected by the tracking service.
"""

from datetime import datetime

from mail_api.shared.utils.metrics_utils import calculate_rate


def process_isp_performance(data: list[dict]) -> list[dict]:
    """Processes per-ISP performance data"""
    processed = []
    for row in data:
        processed.append({
            "domain": row["isp"],  # Renamed to stay compatible
            "sent": row["sent"],
            "delivered": row["delivered"],
            "bounced": row["bounced"],
            "deferred": row.get("deferred", 0),
            "failed": row.get("failed", 0),
            "delivery_rate": calculate_rate(row["delivered"], row["sent"]),
            "bounce_rate": calculate_rate(row["bounced"], row["sent"]),
            "deferred_rate": calculate_rate(row.get("deferred", 0), row["sent"]),
            "failed_rate": calculate_rate(row.get("failed", 0), row["sent"])
        })
    return processed


def process_timeseries_data(data: list[dict]) -> list[dict]:
    """Processes time series data"""
    processed = []
    for row in data:
        processed.append({
            "date": row["date"].strftime('%Y-%m-%d') if isinstance(row["date"], datetime) else str(row["date"]),
            "sent": row["sent"],
            "delivered": row["delivered"],
            "bounced": row["bounced"],
            "deferred": row.get("deferred", 0),
            "failed": row.get("failed", 0),
            "delivery_rate": calculate_rate(row["delivered"], row["sent"]),
            "bounce_rate": calculate_rate(row["bounced"], row["sent"]),
            "deferred_rate": calculate_rate(row.get("deferred", 0), row["sent"]),
            "failed_rate": calculate_rate(row.get("failed", 0), row["sent"])
        })
    return processed


def process_bounce_breakdown(bounce_data: dict, total_sent: int) -> dict:
    """
    Processes the bounce breakdown

    Args:
        bounce_data: Raw bounce data
        total_sent: Total emails sent

    Returns:
        Dict: Processed breakdown with the computed rates
    """
    hard_bounces = bounce_data.get("hard_bounces", 0) or 0
    soft_bounces = bounce_data.get("soft_bounces", 0) or 0
    policy_bounces = bounce_data.get("policy_bounces", 0) or 0
    other_bounces = bounce_data.get("other_bounces", 0) or 0

    return {
        "hard_bounces": hard_bounces,
        "soft_bounces": soft_bounces,
        "policy_bounces": policy_bounces,
        "other_bounces": other_bounces,
        "hard_bounce_rate": calculate_rate(hard_bounces, total_sent),
        "soft_bounce_rate": calculate_rate(soft_bounces, total_sent),
        "policy_bounce_rate": calculate_rate(policy_bounces, total_sent),
        "other_bounce_rate": calculate_rate(other_bounces, total_sent),
    }
