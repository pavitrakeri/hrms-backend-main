"""
Payroll V2 Read-Only Data Ingestion Adapters.

Strict adherence to AGENT.md Rule 2:
  - Read-only parameterized SQL (`SELECT` only)
  - Zero edits to upstream modules (`attendance.py`, `leaves.py`, `reimbursement/`)
"""

from app.features.payroll_v2.adapters.advance_adapter import (
    AdvanceLoanRow,
    EmployeeAdvanceSummary,
    ScheduledAdvanceRecovery,
    compute_advance_summary,
    fetch_advances_batch,
    fetch_employee_advances,
)
from app.features.payroll_v2.adapters.attendance_adapter import (
    EmployeeAttendanceSummary,
    compute_attendance_summary,
    fetch_attendance_batch,
    fetch_employee_attendance,
)
from app.features.payroll_v2.adapters.calendar_service import (
    IndianCalendarSummary,
    build_indian_calendar_summary,
    fetch_indian_calendar,
    parse_weekend_days,
)
from app.features.payroll_v2.adapters.employee_aggregator import (
    CycleIngestionResult,
    EmployeeIngestionBundle,
    fetch_cycle_employee_inputs,
    get_financial_year_label,
    select_latest_pt_slabs,
)
from app.features.payroll_v2.adapters.expense_adapter import (
    ApprovedReimbursementItem,
    EmployeeExpenseSummary,
    aggregate_employee_expenses,
    fetch_employee_pending_reimbursements,
    fetch_reimbursements_batch,
)
from app.features.payroll_v2.adapters.leave_adapter import (
    EmployeeLeaveSummary,
    LeaveRecord,
    aggregate_employee_leaves,
    fetch_employee_leaves,
    fetch_leaves_batch,
)

__all__ = [
    "AdvanceLoanRow",
    "ApprovedReimbursementItem",
    "CycleIngestionResult",
    "EmployeeAdvanceSummary",
    "EmployeeAttendanceSummary",
    "EmployeeExpenseSummary",
    "EmployeeIngestionBundle",
    "EmployeeLeaveSummary",
    "IndianCalendarSummary",
    "LeaveRecord",
    "ScheduledAdvanceRecovery",
    "aggregate_employee_expenses",
    "aggregate_employee_leaves",
    "build_indian_calendar_summary",
    "compute_advance_summary",
    "compute_attendance_summary",
    "fetch_advances_batch",
    "fetch_attendance_batch",
    "fetch_cycle_employee_inputs",
    "fetch_employee_advances",
    "fetch_employee_attendance",
    "fetch_employee_leaves",
    "fetch_employee_pending_reimbursements",
    "fetch_indian_calendar",
    "fetch_leaves_batch",
    "fetch_reimbursements_batch",
    "get_financial_year_label",
    "parse_weekend_days",
    "select_latest_pt_slabs",
]
