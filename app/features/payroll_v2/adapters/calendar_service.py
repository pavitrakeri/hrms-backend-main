"""
Indian Calendar Service (Read-Only Adapter).

Reads `company_settings.weekend_days` and `holidays` from PostgreSQL to distinguish
scheduled working days from paid weekly rest days and Indian gazetted holidays
(fixes legacy defect DEF-03's hardcoded Fri/Sat UAE weekend).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable, Optional, Set

from pydantic import BaseModel, Field

_DAY_NAME_TO_WEEKDAY = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}

# Standard Indian corporate weekly rest days (Saturday=5, Sunday=6)
DEFAULT_INDIAN_WEEKEND_WEEKDAYS: frozenset[int] = frozenset({5, 6})


def parse_weekend_days(weekend_str: Optional[str]) -> set[int]:
    """
    Parse comma-separated weekday names (e.g., 'Saturday,Sunday') into Python
    `date.weekday()` integers (Monday=0 .. Sunday=6). Falls back to {5, 6}.
    """
    if not weekend_str or not weekend_str.strip():
        return set(DEFAULT_INDIAN_WEEKEND_WEEKDAYS)

    parsed: set[int] = set()
    for token in weekend_str.split(","):
        key = token.strip().lower()
        if key in _DAY_NAME_TO_WEEKDAY:
            parsed.add(_DAY_NAME_TO_WEEKDAY[key])

    return parsed if parsed else set(DEFAULT_INDIAN_WEEKEND_WEEKDAYS)


def iter_dates(start_date: date, end_date: date) -> Iterable[date]:
    """Yield inclusive dates from `start_date` through `end_date`."""
    cur = start_date
    while cur <= end_date:
        yield cur
        cur += timedelta(days=1)


class IndianCalendarSummary(BaseModel):
    start_date: date
    end_date: date
    total_calendar_days: int
    weekend_weekdays: list[int] = Field(default_factory=lambda: [5, 6])
    weekend_dates: set[date] = Field(default_factory=set)
    holiday_dates: set[date] = Field(default_factory=set)
    holiday_names: dict[date, str] = Field(default_factory=dict)
    non_working_dates: set[date] = Field(default_factory=set)
    scheduled_working_dates: list[date] = Field(default_factory=list)


def build_indian_calendar_summary(
    *,
    start_date: date,
    end_date: date,
    weekend_days_str: Optional[str] = "Saturday,Sunday",
    holidays: Optional[Iterable[tuple[date, str]]] = None,
) -> IndianCalendarSummary:
    """Pure calendar builder used by both async DB adapter and offline unit tests."""
    if end_date < start_date:
        raise ValueError(f"end_date ({end_date}) cannot be earlier than start_date ({start_date}).")

    weekend_weekdays = parse_weekend_days(weekend_days_str)
    holiday_map: dict[date, str] = {}
    if holidays:
        for h_date, h_name in holidays:
            if start_date <= h_date <= end_date:
                holiday_map[h_date] = h_name

    weekend_dates: set[date] = set()
    scheduled_working: list[date] = []
    holiday_dates: Set[date] = set(holiday_map.keys())

    all_days = list(iter_dates(start_date, end_date))
    for d in all_days:
        is_weekend = d.weekday() in weekend_weekdays
        if is_weekend:
            weekend_dates.add(d)
        if not is_weekend and d not in holiday_dates:
            scheduled_working.append(d)

    non_working = weekend_dates | holiday_dates
    return IndianCalendarSummary(
        start_date=start_date,
        end_date=end_date,
        total_calendar_days=len(all_days),
        weekend_weekdays=sorted(weekend_weekdays),
        weekend_dates=weekend_dates,
        holiday_dates=set(holiday_dates),
        holiday_names=holiday_map,
        non_working_dates=non_working,
        scheduled_working_dates=scheduled_working,
    )


async def fetch_indian_calendar(
    conn,
    *,
    start_date: date,
    end_date: date,
) -> IndianCalendarSummary:
    """
    Read company weekend policy (`company_settings`) and gazetted holidays
    (`holidays`) strictly read-only for the given billing window.
    """
    settings_row = await conn.fetchrow(
        "SELECT weekend_days FROM company_settings LIMIT 1;"
    )
    weekend_str = settings_row["weekend_days"] if settings_row else "Saturday,Sunday"

    holiday_rows = await conn.fetch(
        """
        SELECT date, name
        FROM holidays
        WHERE date >= $1 AND date <= $2
        ORDER BY date ASC;
        """,
        start_date,
        end_date,
    )
    holidays = [(r["date"], r["name"]) for r in holiday_rows]

    return build_indian_calendar_summary(
        start_date=start_date,
        end_date=end_date,
        weekend_days_str=weekend_str,
        holidays=holidays,
    )
