"""
Attendance Ingestion Adapter (Read-Only).

Reads clock-ins from `attendance` (converted to IST `Asia/Kolkata` dates) and
approved corrections from `attendance_regularisations`, cross-referencing the
Indian Calendar Service, approved leaves, and employee joining/exit dates.
"""

from __future__ import annotations

from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional

from pydantic import BaseModel, Field

from app.features.payroll_v2.adapters.calendar_service import IndianCalendarSummary, iter_dates
from app.features.payroll_v2.adapters.leave_adapter import EmployeeLeaveSummary

ZERO = Decimal("0")
ONE = Decimal("1")
IST_OFFSET = timezone(timedelta(hours=5, minutes=30))


def _today_ist() -> date:
    return datetime.now(IST_OFFSET).date()


class EmployeeAttendanceSummary(BaseModel):
    employee_id: str
    present_dates_count: int = 0
    non_employed_days: Decimal = ZERO
    unpaid_leave_days: Decimal = ZERO
    unexcused_absent_days: Decimal = ZERO
    raw_missing_punch_days: Decimal = ZERO
    absent_dates: list[date] = Field(default_factory=list)


def compute_attendance_summary(
    *,
    employee_id: str,
    calendar_summary: IndianCalendarSummary,
    leave_summary: EmployeeLeaveSummary,
    present_dates: Iterable[date],
    cutoff_date: date,
    joining_date: Optional[date] = None,
    date_of_exit: Optional[date] = None,
    as_of_date: Optional[date] = None,
    strict_attendance: bool = True,
) -> EmployeeAttendanceSummary:
    """
    Pure attendance evaluator combining calendar rest days, approved leaves,
    clock-in/regularisation dates, and employment window `[joining_date, date_of_exit]`.
    """
    period_start = calendar_summary.start_date
    period_end = calendar_summary.end_date
    cal_days_dec = Decimal(calendar_summary.total_calendar_days)

    active_start = max(period_start, joining_date) if joining_date else period_start
    active_end = min(period_end, date_of_exit) if date_of_exit else period_end

    # Count calendar days outside active employment window
    if active_end < period_start or active_start > period_end or active_end < active_start:
        return EmployeeAttendanceSummary(
            employee_id=employee_id,
            present_dates_count=0,
            non_employed_days=cal_days_dec,
            unpaid_leave_days=cal_days_dec,
            unexcused_absent_days=ZERO,
            raw_missing_punch_days=ZERO,
            absent_dates=[],
        )

    employed_calendar_days = (active_end - active_start).days + 1
    non_employed_days = Decimal(max(0, calendar_summary.total_calendar_days - employed_calendar_days))

    # Only inspect working days up to cutoff_date and not in the future
    ref_today = as_of_date or _today_ist()
    observation_end = min(active_end, cutoff_date, ref_today)

    present_set = {d for d in present_dates if active_start <= d <= active_end}

    missing_weight_total = ZERO
    absent_dates: list[date] = []

    for d in calendar_summary.scheduled_working_dates:
        if d < active_start or d > observation_end:
            continue
        if d in present_set:
            continue

        paid_w = leave_summary.paid_leave_weights.get(d, ZERO)
        unpaid_w = leave_summary.unpaid_leave_weights.get(d, ZERO)
        covered_by_leave = min(ONE, paid_w + unpaid_w)
        unaccounted = max(ZERO, ONE - covered_by_leave)

        if unaccounted > ZERO:
            missing_weight_total += unaccounted
            absent_dates.append(d)

    # Filter unpaid leaves strictly within active employment dates
    active_unpaid_leaves = sum(
        (
            w
            for d, w in leave_summary.unpaid_leave_weights.items()
            if active_start <= d <= active_end
        ),
        ZERO,
    )

    total_unpaid = min(cal_days_dec, non_employed_days + active_unpaid_leaves)
    raw_missing = missing_weight_total
    unexcused_absent = (
        min(raw_missing, max(ZERO, cal_days_dec - total_unpaid))
        if strict_attendance
        else ZERO
    )

    return EmployeeAttendanceSummary(
        employee_id=employee_id,
        present_dates_count=len(present_set),
        non_employed_days=non_employed_days,
        unpaid_leave_days=total_unpaid,
        unexcused_absent_days=unexcused_absent,
        raw_missing_punch_days=raw_missing,
        absent_dates=absent_dates,
    )


async def fetch_employee_attendance(
    conn,
    *,
    employee_id: str,
    calendar_summary: IndianCalendarSummary,
    leave_summary: EmployeeLeaveSummary,
    cutoff_date: date,
    joining_date: Optional[date] = None,
    date_of_exit: Optional[date] = None,
    as_of_date: Optional[date] = None,
    strict_attendance: bool = True,
) -> EmployeeAttendanceSummary:
    """Fetch clock-ins + approved regularisations for one employee (read-only)."""
    clock_rows = await conn.fetch(
        """
        SELECT DISTINCT (clock_in_at AT TIME ZONE 'Asia/Kolkata')::date AS work_date
        FROM attendance
        WHERE user_id = $1::uuid
          AND clock_in_at IS NOT NULL
          AND (clock_in_at AT TIME ZONE 'Asia/Kolkata')::date >= $2
          AND (clock_in_at AT TIME ZONE 'Asia/Kolkata')::date <= $3;
        """,
        employee_id,
        calendar_summary.start_date,
        calendar_summary.end_date,
    )
    reg_rows = await conn.fetch(
        """
        SELECT DISTINCT request_date AS work_date
        FROM attendance_regularisations
        WHERE user_id = $1::uuid
          AND status = 'approved'
          AND request_date >= $2
          AND request_date <= $3;
        """,
        employee_id,
        calendar_summary.start_date,
        calendar_summary.end_date,
    )

    present_dates = {r["work_date"] for r in clock_rows if r["work_date"]} | {
        r["work_date"] for r in reg_rows if r["work_date"]
    }

    return compute_attendance_summary(
        employee_id=employee_id,
        calendar_summary=calendar_summary,
        leave_summary=leave_summary,
        present_dates=present_dates,
        cutoff_date=cutoff_date,
        joining_date=joining_date,
        date_of_exit=date_of_exit,
        as_of_date=as_of_date,
        strict_attendance=strict_attendance,
    )


async def fetch_attendance_batch(
    conn,
    *,
    employee_meta: list[dict[str, Any]],
    calendar_summary: IndianCalendarSummary,
    leaves_by_emp: Mapping[str, EmployeeLeaveSummary],
    cutoff_date: date,
    as_of_date: Optional[date] = None,
    strict_attendance: bool = True,
) -> Mapping[str, EmployeeAttendanceSummary]:
    """
    Batch-fetch clock-ins and approved regularisations for multiple employees.
    Each item in `employee_meta` must have `employee_id`, `joining_date`, `date_of_exit`.
    """
    if not employee_meta:
        return {}

    emp_ids = [m["employee_id"] for m in employee_meta]

    clock_rows = await conn.fetch(
        """
        SELECT DISTINCT user_id::text AS employee_id,
                        (clock_in_at AT TIME ZONE 'Asia/Kolkata')::date AS work_date
        FROM attendance
        WHERE user_id = ANY($1::uuid[])
          AND clock_in_at IS NOT NULL
          AND (clock_in_at AT TIME ZONE 'Asia/Kolkata')::date >= $2
          AND (clock_in_at AT TIME ZONE 'Asia/Kolkata')::date <= $3;
        """,
        emp_ids,
        calendar_summary.start_date,
        calendar_summary.end_date,
    )
    reg_rows = await conn.fetch(
        """
        SELECT DISTINCT user_id::text AS employee_id,
                        request_date AS work_date
        FROM attendance_regularisations
        WHERE user_id = ANY($1::uuid[])
          AND status = 'approved'
          AND request_date >= $2
          AND request_date <= $3;
        """,
        emp_ids,
        calendar_summary.start_date,
        calendar_summary.end_date,
    )

    present_map: dict[str, set[date]] = {eid: set() for eid in emp_ids}
    for r in clock_rows:
        if r["employee_id"] in present_map and r["work_date"]:
            present_map[r["employee_id"]].add(r["work_date"])
    for r in reg_rows:
        if r["employee_id"] in present_map and r["work_date"]:
            present_map[r["employee_id"]].add(r["work_date"])

    results: dict[str, EmployeeAttendanceSummary] = {}
    for meta in employee_meta:
        eid = meta["employee_id"]
        leave_sum = leaves_by_emp.get(eid) or EmployeeLeaveSummary(employee_id=eid)
        results[eid] = compute_attendance_summary(
            employee_id=eid,
            calendar_summary=calendar_summary,
            leave_summary=leave_sum,
            present_dates=present_map[eid],
            cutoff_date=cutoff_date,
            joining_date=meta.get("joining_date"),
            date_of_exit=meta.get("date_of_exit"),
            as_of_date=as_of_date,
            strict_attendance=strict_attendance,
        )
    return results
