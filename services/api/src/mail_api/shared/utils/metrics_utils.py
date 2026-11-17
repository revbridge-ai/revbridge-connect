"""
Metric calculation utilities

Common functions to compute rates, trends and pagination.
"""

from typing import Any


def calculate_rate(numerator: int, denominator: int) -> float:
    """Percentage rate, safe against division by zero"""
    if denominator == 0:
        return 0.0
    return round((numerator / denominator) * 100, 2)


def determine_trend(current: float, previous: float) -> str:
    """Trend from comparing two values"""
    if current > previous:
        return "up"
    elif current < previous:
        return "down"
    else:
        return "stable"


def calculate_pagination(page: int, per_page: int, total: int) -> dict[str, Any]:
    """Computes pagination metadata"""
    total_pages = (total + per_page - 1) // per_page
    offset = (page - 1) * per_page

    return {
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "offset": offset,
        "has_next": page < total_pages,
        "has_prev": page > 1
    }


def build_pagination_metadata(page: int, per_page: int, total: int) -> dict[str, Any]:
    """Builds the pagination metadata block"""
    pagination = calculate_pagination(page, per_page, total)

    return {
        "pagination": {
            "page": pagination["page"],
            "per_page": pagination["per_page"],
            "total": pagination["total"],
            "total_pages": pagination["total_pages"],
            "has_next": pagination["has_next"],
            "has_prev": pagination["has_prev"]
        }
    }
