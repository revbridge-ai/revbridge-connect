"""
Date handling utilities

Common functions to parse, validate and manipulate dates.
"""

from datetime import datetime, timedelta


def get_default_period(days: int = 30) -> tuple[str, str]:
    """
    Returns the default period (last N days)

    Args:
        days: Number of days back

    Returns:
        Tuple: (start_date, end_date) as YYYY-MM-DD
    """
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)

    return start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")


def parse_date_range(
    start_date: str | None, end_date: str | None, default_days: int = 30
) -> tuple[str, str]:
    """
    Parses and validates dates, falling back to the default period when needed

    Args:
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        default_days: Default number of days when not given

    Returns:
        Tuple: validated (start_date, end_date)
    """
    if not start_date:
        start_date = (datetime.now() - timedelta(days=default_days)).strftime('%Y-%m-%d')
    if not end_date:
        end_date = datetime.now().strftime('%Y-%m-%d')

    try:
        start_dt = datetime.strptime(start_date, '%Y-%m-%d')
        end_dt = datetime.strptime(end_date, '%Y-%m-%d')

        # Make end_date the end of the day
        end_dt = end_dt.replace(hour=23, minute=59, second=59, microsecond=999999)

        return start_dt, end_dt
    except ValueError as e:
        raise ValueError(f"Invalid date format: {e}") from e


def calculate_period_halves(start_date: datetime, end_date: datetime) -> tuple[datetime, datetime]:
    """Computes the midpoint of the period, for comparisons"""
    period_duration = end_date - start_date
    half_duration = period_duration / 2
    midpoint = start_date + half_duration
    return start_date, midpoint


def _to_date(d: str | datetime) -> datetime:
    """Converts a string or datetime to datetime"""
    if isinstance(d, datetime):
        return d
    return datetime.strptime(d, "%Y-%m-%d")


def _date_iso(d: datetime) -> str:
    """Converts a datetime to an ISO date string"""
    return d.strftime("%Y-%m-%d")


def _split_period_in_halves(
    start_d: datetime, end_d: datetime
) -> tuple[datetime, datetime, datetime, datetime]:
    """
    Splits a period into two non-overlapping halves

    Args:
        start_d: Start date
        end_d: End date

    Returns:
        Tuple: (first_start, first_end, second_start, second_end)
    """
    if end_d < start_d:
        raise ValueError("end_date < start_date")
    total_days = (end_d - start_d).days + 1  # inclusive
    half = total_days // 2
    first_end = start_d + timedelta(days=half - 1) if half > 0 else start_d
    second_start = first_end + timedelta(days=1)
    if second_start > end_d:  # single-day period
        second_start = first_end  # both halves point to the same day
    return start_d, first_end, second_start, end_d
