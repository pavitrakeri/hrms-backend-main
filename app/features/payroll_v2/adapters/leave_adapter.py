"""
Leave Ingestion Adapter (Read-Only).

Reads approved leave requests from `leaves` joined with `leave_types` and maps
each covered working day in `[start_date, end_date]` into either paid leave
(excused from clock-in) or unpaid Loss-of-Pay (NCP) days.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional

from pydantic import BaseModel, Field

from app.features.payroll_v2.adapters.calendar_service import IndianCalendarSummary, iter_dates

ZERO = Decimal("0")
HALF = Decimal("0.5")
ONE = Decimal("1")

UNPAID_LEAVE_TYPE_NAMES: frozenset[str] = frozenset(
    {"unpaid", "lop", "loss of pay", "lwp", "leave without pay"}
)


class LeaveRecord(BaseModel):
    leave_type: str
    start_date: date
    end_date: date
    half_day: bool = False


class EmployeeLeaveSummary(BaseModel):
    employee_id: str
    paid_leave_days: Decimal = ZERO
    unpaid_leave_days: Decimal = ZERO
    paid_leave_weights: dict[date, Decimal] = Field(default_factory=dict)
    unpaid_leave_weights: dict[date, Decimal] = Field(default_factory=dict)


def _is_unpaid_leave_type(leave_type: str) -> bool:
    normalized = (leave_type or "").strip().lower()
    return normalized in UNPAID_LEAVE_TYPE_NAMES


def aggregate_employee_leaves(
    *,
    employee_id: str,
    records: Iterable[LeaveRecord],
    calendar_summary: IndianCalendarSummary,
) -> EmployeeLeaveSummary:
    """
    Aggregate overlapping approved leave records onto scheduled working days only.
    Handles month-boundary leaves and `half_day=True` cleanly without double-counting.
    """
    paid_weights: dict[date, Decimal] = {}
    unpaid_weights: dict[date, Decimal] = {}
    non_working = calendar_summary.non_working_dates

    for rec in records:
        if rec.end_date < rec.start_date:
            continue
        window_start = max(rec.start_date, calendar_summary.start_date)
        window_end = min(rec.end_date, calendar_summary.end_date)
        if window_end < window_start:
            continue

        is_unpaid = _is_unpaid_leave_type(rec.leave_type)
        target_map = unpaid_weights if is_unpaid else paid_weights

        for d in iter_dates(window_start, window_end):
            if d in non_working:
                continue
            # Single-day half_day=True or trailing half-day on multi-day leave
            weight = HALF if (rec.half_day and d == rec.end_date) else ONE
            existing = target_map.get(d, ZERO)
            target_map[d] = min(ONE, existing + weight)

    # Guard against overlapping paid + unpaid leave records exceeding 1.0 on a single date
    for d in list(unpaid_weights.keys()):
        paid_on_d = paid_weights.get(d, ZERO)
        if paid_on_d + unpaid_weights[d] > ONE:
            adjusted = max(ZERO, ONE - paid_on_d)
            if adjusted == ZERO:
                del unpaid_weights[d]
            else:
                unpaid_weights[d] = adjusted

    paid_total = sum(paid_weights.values(), ZERO)
    unpaid_total = sum(unpaid_weights.values(), ZERO)

    return EmployeeLeaveSummary(
        employee_id=employee_id,
        paid_leave_days=paid_total,
        unpaid_leave_days=unpaid_total,
        paid_leave_weights=paid_weights,
        unpaid_leave_weights=unpaid_weights,
    )


async def fetch_employee_leaves(
    conn,
    *,
    employee_id: str,
    calendar_summary: IndianCalendarSummary,
) -> EmployeeLeaveSummary:
    """Fetch approved overlapping leave requests for one employee (read-only)."""
    rows = await conn.fetch(
        """
        SELECT lt.name AS leave_type, l.start_date, l.end_date, COALESCE(l.half_day, false) AS half_day
        FROM leaves l
        JOIN leave_types lt ON l.leave_type_id = lt.id
        WHERE l.user_id = $1::uuid
          AND l.status = 'approved'
          AND l.start_date <= $3
          AND l.end_date >= $2
        ORDER BY l.start_date ASC;
        """,
        employee_id,
        calendar_summary.start_date,
        calendar_summary.end_date,
    )
    records = [
        LeaveRecord(
            leave_type=r["leave_type"],
            start_date=r["start_date"],
            end_date=r["end_date"],
            half_day=bool(r["half_day"]),
        )
        for r in rows
    ]
    return aggregate_employee_leaves(
        employee_id=employee_id,
        records=records,
        calendar_summary=calendar_summary,
    )


async def fetch_leaves_batch(
    conn,
    *,
    employee_ids: list[str],
    calendar_summary: IndianCalendarSummary,
) -> Mapping[str, EmployeeLeaveSummary]:
    """Batch-fetch approved leaves for a list of employees in one query (read-only)."""
    if not employee_ids:
        return {}

    rows: list[Any] = await conn.fetch(
        """
        SELECT l.user_id::text AS employee_id,
               lt.name AS leave_type,
               l.start_date,
               l.end_date,
               COALESCE(l.half_day, false) AS half_day
        FROM leaves l
        JOIN leave_types lt ON l.leave_type_id = lt.id
        WHERE l.user_id = ANY($1::uuid[])
          AND l.status = 'approved'
          AND l.start_date <= $3
          AND l.end_date >= $2
        ORDER BY l.user_id, l.start_date ASC;
        """,
        employee_ids,
        calendar_summary.start_date,
        calendar_summary.end_date,
    )

    grouped: dict[str, list[LeaveRecord]] = {eid: [] for eid in employee_ids}
    for r in rows:
        eid = r["employee_id"]
        if eid in grouped:
            grouped[eid].append(
                LeaveRecord(
                    leave_type=r["leave_type"],
                    start_date=r["start_date"],
                    end_date=r["end_date"],
                    half_day=bool(r["half_day"]),
                )
            )

    return {
        eid: aggregate_employee_leaves(
            employee_id=eid,
            records=grouped[eid],
            calendar_summary=calendar_summary,
        )
        for eid in employee_ids
    }
