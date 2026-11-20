"""
Payroll Input Aggregator (Read-Only Orchestrator for Phase 3 Adapters).

Combines read-only fetches from:
  - `employee_salary_structures` & `users`
  - `employee_tax_declarations`
  - `pt_state_slabs`
  - `employee_bank_details` (statutory readiness warnings)
  - Prior `DISBURSED` `payroll_entries` (FY cumulative YTD true-up)
  - Calendar, Leave, Attendance, Expense & Salary Advance adapters

Assembles validated `PayrollEmployeeInput` bundles ready for `engine_india.py`.
Contains strictly parameterized `SELECT` queries — ZERO writes to PostgreSQL.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.features.payroll_v2.adapters.advance_adapter import (
    EmployeeAdvanceSummary,
    ScheduledAdvanceRecovery,
    fetch_advances_batch,
)
from app.features.payroll_v2.adapters.attendance_adapter import (
    EmployeeAttendanceSummary,
    fetch_attendance_batch,
)
from app.features.payroll_v2.adapters.calendar_service import (
    IndianCalendarSummary,
    fetch_indian_calendar,
)
from app.features.payroll_v2.adapters.expense_adapter import (
    EmployeeExpenseSummary,
    fetch_reimbursements_batch,
)
from app.features.payroll_v2.adapters.leave_adapter import (
    EmployeeLeaveSummary,
    fetch_leaves_batch,
)
from app.features.payroll_v2.engine_india import (
    DEFAULT_PRORATION_DIVISOR,
    PTSlab,
    PayrollEmployeeInput,
    TaxDeclarationInput,
)

ZERO = Decimal("0")


def get_financial_year_label(cycle_month: int, cycle_year: int) -> tuple[str, int, int]:
    """
    Return `(fy_label, fy_start_year, fy_end_year)` for Indian Financial Year (Apr-Mar).
    Example: Nov 2026 -> ('2026-2027', 2026, 2027); Feb 2027 -> ('2026-2027', 2026, 2027).
    """
    fy_start = cycle_year if cycle_month >= 4 else cycle_year - 1
    fy_end = fy_start + 1
    return f"{fy_start}-{fy_end}", fy_start, fy_end


def select_latest_pt_slabs(raw_rows: list[Any]) -> list[PTSlab]:
    """
    Keep only the latest `effective_from` slab set per `state_code` and deduplicate
    identical `(state_code, min_monthly_gross)` brackets cleanly.
    """
    latest_date_by_state: dict[str, date] = {}
    for r in raw_rows:
        st = str(r["state_code"]).strip().upper()
        eff = r["effective_from"]
        if st not in latest_date_by_state or eff > latest_date_by_state[st]:
            latest_date_by_state[st] = eff

    seen_keys: set[tuple[str, Decimal]] = set()
    slabs: list[PTSlab] = []
    for r in raw_rows:
        st = str(r["state_code"]).strip().upper()
        if r["effective_from"] != latest_date_by_state.get(st):
            continue
        min_gross = Decimal(str(r["min_monthly_gross"]))
        key = (st, min_gross)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        slabs.append(
            PTSlab(
                state_code=st,
                state_name=str(r["state_name"]),
                min_monthly_gross=min_gross,
                max_monthly_gross=(
                    Decimal(str(r["max_monthly_gross"]))
                    if r["max_monthly_gross"] is not None
                    else None
                ),
                pt_amount=Decimal(str(r["pt_amount"])),
                special_feb_amount=(
                    Decimal(str(r["special_feb_amount"]))
                    if r["special_feb_amount"] is not None
                    else None
                ),
            )
        )
    return slabs


class EmployeeIngestionBundle(BaseModel):
    employee_id: str
    employee_name: str
    email: str
    engine_input: PayrollEmployeeInput
    reimbursement_ids: list[str] = Field(default_factory=list)
    advance_recoveries: list[ScheduledAdvanceRecovery] = Field(default_factory=list)
    attendance_summary: EmployeeAttendanceSummary
    leave_summary: EmployeeLeaveSummary
    expense_summary: EmployeeExpenseSummary
    advance_summary: EmployeeAdvanceSummary
    missing_statutory_warnings: list[str] = Field(default_factory=list)


class CycleIngestionResult(BaseModel):
    cycle_month: int
    cycle_year: int
    financial_year: str
    start_date: date
    end_date: date
    cutoff_date: date
    calendar_summary: IndianCalendarSummary
    bundles: list[EmployeeIngestionBundle] = Field(default_factory=list)


async def fetch_cycle_employee_inputs(
    conn,
    *,
    cycle_month: int,
    cycle_year: int,
    start_date: date,
    end_date: date,
    cutoff_date: date,
    employee_ids: Optional[list[str]] = None,
    reference_cycle_id: Optional[str] = None,
    proration_divisor: Decimal = DEFAULT_PRORATION_DIVISOR,
    as_of_date: Optional[date] = None,
    strict_attendance: bool = True,
) -> CycleIngestionResult:
    """
    Assemble complete calculation-ready input bundles for all eligible active employees
    (or held employees from `reference_cycle_id` for `OFF_CYCLE` / `SUPPLEMENTAL` runs).
    """
    fy_label, fy_start, fy_end = get_financial_year_label(cycle_month, cycle_year)

    # 1. Fetch active employees & salary structures
    conditions = ["ess.is_active = true", "u.is_active = true"]
    params: list[Any] = []
    idx = 1

    if employee_ids is not None:
        if not employee_ids:
            cal = await fetch_indian_calendar(conn, start_date=start_date, end_date=end_date)
            return CycleIngestionResult(
                cycle_month=cycle_month,
                cycle_year=cycle_year,
                financial_year=fy_label,
                start_date=start_date,
                end_date=end_date,
                cutoff_date=cutoff_date,
                calendar_summary=cal,
                bundles=[],
            )
        conditions.append(f"u.id = ANY(${idx}::uuid[])")
        params.append(employee_ids)
        idx += 1

    if reference_cycle_id:
        conditions.append(
            f"""u.id IN (
                SELECT pe.employee_id
                FROM payroll_entries pe
                WHERE pe.cycle_id = ${idx}::uuid
                  AND (pe.status = 'ON_HOLD' OR pe.payout_status != 'PAID')
            )"""
        )
        params.append(reference_cycle_id)
        idx += 1

        # Guard against paying someone already PAID in any DISBURSED cycle of the same month
        conditions.append(
            f"""u.id NOT IN (
                SELECT pe2.employee_id
                FROM payroll_entries pe2
                JOIN payroll_cycles pc2 ON pc2.id = pe2.cycle_id
                WHERE pc2.cycle_month = ${idx}
                  AND pc2.cycle_year = ${idx + 1}
                  AND pc2.status = 'DISBURSED'
                  AND pe2.status = 'CLEARED'
                  AND pe2.payout_status = 'PAID'
            )"""
        )
        params.extend([cycle_month, cycle_year])
        idx += 2

    emp_sql = f"""
        SELECT
            u.id::text AS employee_id,
            u.full_name AS employee_name,
            u.email,
            u.joining_date,
            u.date_of_exit,
            u.pan_number,
            u.uan_number,
            u.esic_ip_number,
            ess.monthly_basic_pay,
            ess.monthly_hra,
            ess.monthly_special_allowance,
            ess.monthly_da,
            ess.is_epf_eligible,
            ess.epf_wage_cap_applied,
            ess.vpf_percentage,
            ess.is_esi_eligible,
            ess.pt_state_code,
            ess.tax_regime
        FROM employee_salary_structures ess
        JOIN users u ON u.id = ess.employee_id
        WHERE {' AND '.join(conditions)}
        ORDER BY u.full_name ASC;
    """
    emp_rows = await conn.fetch(emp_sql, *params)

    cal_summary = await fetch_indian_calendar(conn, start_date=start_date, end_date=end_date)
    if not emp_rows:
        return CycleIngestionResult(
            cycle_month=cycle_month,
            cycle_year=cycle_year,
            financial_year=fy_label,
            start_date=start_date,
            end_date=end_date,
            cutoff_date=cutoff_date,
            calendar_summary=cal_summary,
            bundles=[],
        )

    fetched_emp_ids = [r["employee_id"] for r in emp_rows]
    emp_meta = [
        {
            "employee_id": r["employee_id"],
            "joining_date": r["joining_date"],
            "date_of_exit": r["date_of_exit"],
        }
        for r in emp_rows
    ]

    # 2. Fetch Company Statutory (Establishment-level ESIC gate), PT slabs, Tax Declarations, Bank details, Prior YTD
    company_row = None
    try:
        company_row = await conn.fetchrow(
            """
            SELECT is_esic_applicable, esic_establishment_code
            FROM company_statutory_details
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 1;
            """
        )
    except Exception:
        company_row = None

    is_company_esic_covered = True
    if company_row is not None:
        try:
            if isinstance(company_row, dict):
                if "is_esic_applicable" in company_row:
                    is_company_esic_covered = bool(company_row["is_esic_applicable"])
                else:
                    is_company_esic_covered = bool(
                        company_row.get("esic_establishment_code")
                        and str(company_row.get("esic_establishment_code")).strip()
                    )
            else:
                try:
                    is_company_esic_covered = bool(company_row["is_esic_applicable"])
                except (KeyError, IndexError, TypeError):
                    is_company_esic_covered = bool(
                        company_row["esic_establishment_code"]
                        and str(company_row["esic_establishment_code"]).strip()
                    )
        except Exception:
            is_company_esic_covered = True

    pt_rows = await conn.fetch(
        """
        SELECT state_code, state_name, effective_from, min_monthly_gross, max_monthly_gross,
               pt_amount, special_feb_amount
        FROM pt_state_slabs
        WHERE effective_from <= $1
        ORDER BY state_code ASC, effective_from DESC, min_monthly_gross ASC;
        """,
        end_date,
    )
    pt_slabs = select_latest_pt_slabs(pt_rows)

    tax_rows = await conn.fetch(
        """
        SELECT employee_id::text AS employee_id,
               regime,
               section_80c_amount,
               section_80d_amount,
               annual_rent_paid,
               home_loan_interest,
               nps_80ccd_1b
        FROM employee_tax_declarations
        WHERE employee_id = ANY($1::uuid[])
          AND financial_year = $2;
        """,
        fetched_emp_ids,
        fy_label,
    )
    tax_by_emp: dict[str, TaxDeclarationInput] = {
        r["employee_id"]: TaxDeclarationInput(
            regime=r["regime"],
            section_80c_amount=Decimal(str(r["section_80c_amount"])),
            section_80d_amount=Decimal(str(r["section_80d_amount"])),
            annual_rent_paid=Decimal(str(r["annual_rent_paid"])),
            home_loan_interest=Decimal(str(r["home_loan_interest"])),
            nps_80ccd_1b=Decimal(str(r["nps_80ccd_1b"])),
        )
        for r in tax_rows
    }

    bank_rows = await conn.fetch(
        """
        SELECT employee_id::text AS employee_id,
               payment_method,
               account_number,
               ifsc_code
        FROM employee_bank_details
        WHERE employee_id = ANY($1::uuid[])
          AND is_primary = true;
        """,
        fetched_emp_ids,
    )
    bank_by_emp = {r["employee_id"]: r for r in bank_rows}

    ytd_rows = await conn.fetch(
        """
        SELECT pe.employee_id::text AS employee_id,
               COUNT(*) AS prior_cycles,
               COALESCE(SUM(pe.gross_earnings), 0) AS ytd_gross,
               COALESCE(SUM(pe.earned_basic + pe.earned_da), 0) AS ytd_basic_da,
               COALESCE(SUM(pe.earned_hra), 0) AS ytd_hra,
               COALESCE(SUM(pe.professional_tax), 0) AS ytd_pt,
               COALESCE(SUM(pe.epf_employee_share), 0) AS ytd_epf,
               COALESCE(SUM(pe.tds_amount), 0) AS ytd_tds
        FROM payroll_entries pe
        JOIN payroll_cycles pc ON pc.id = pe.cycle_id
        WHERE pe.employee_id = ANY($1::uuid[])
          AND pc.status = 'DISBURSED'
          AND pe.status = 'CLEARED'
          AND (
              (pc.cycle_year = $2 AND pc.cycle_month >= 4)
              OR (pc.cycle_year = $3 AND pc.cycle_month <= 3)
          )
          AND (pc.cycle_year * 100 + pc.cycle_month) < ($4 * 100 + $5)
        GROUP BY pe.employee_id;
        """,
        fetched_emp_ids,
        fy_start,
        fy_end,
        cycle_year,
        cycle_month,
    )
    ytd_by_emp = {r["employee_id"]: r for r in ytd_rows}

    # 3. Upstream Adapter Batch Ingestion
    leaves_by_emp = await fetch_leaves_batch(
        conn, employee_ids=fetched_emp_ids, calendar_summary=cal_summary
    )
    att_by_emp = await fetch_attendance_batch(
        conn,
        employee_meta=emp_meta,
        calendar_summary=cal_summary,
        leaves_by_emp=leaves_by_emp,
        cutoff_date=cutoff_date,
        as_of_date=as_of_date,
        strict_attendance=strict_attendance,
    )
    exp_by_emp = await fetch_reimbursements_batch(
        conn, employee_ids=fetched_emp_ids, cutoff_date=cutoff_date
    )
    adv_by_emp = await fetch_advances_batch(conn, employee_ids=fetched_emp_ids)

    # 4. Assemble PayrollEmployeeInput Bundles
    bundles: list[EmployeeIngestionBundle] = []
    for r in emp_rows:
        eid = r["employee_id"]
        leave_sum = leaves_by_emp.get(eid) or EmployeeLeaveSummary(employee_id=eid)
        att_sum = att_by_emp[eid]
        exp_sum = exp_by_emp.get(eid) or EmployeeExpenseSummary(employee_id=eid)
        adv_sum = adv_by_emp.get(eid) or EmployeeAdvanceSummary(employee_id=eid)
        ytd_row = ytd_by_emp.get(eid)

        is_emp_esi = is_company_esic_covered and bool(r["is_esi_eligible"])

        warnings: list[str] = []
        if not r["pan_number"]:
            warnings.append("Missing PAN (required for Form 24Q / TDS)")
        if bool(r["is_epf_eligible"]) and not r["uan_number"]:
            warnings.append("Missing 12-digit UAN (required for EPFO ECR)")
        if is_emp_esi and not r["esic_ip_number"]:
            warnings.append("Missing ESIC IP Number")
        bank = bank_by_emp.get(eid)
        if not bank:
            warnings.append("Missing primary bank account details")
        elif bank["payment_method"] == "BANK_TRANSFER" and len((bank["ifsc_code"] or "").strip()) != 11:
            warnings.append("Invalid IFSC code length (must be 11 characters)")

        has_prior_ytd = ytd_row is not None and int(ytd_row["prior_cycles"]) > 0

        emp_input = PayrollEmployeeInput(
            employee_id=eid,
            employee_name=r["employee_name"],
            cycle_month=cycle_month,
            cycle_year=cycle_year,
            total_calendar_days=cal_summary.total_calendar_days,
            proration_divisor=proration_divisor,
            monthly_basic_pay=Decimal(str(r["monthly_basic_pay"])),
            monthly_hra=Decimal(str(r["monthly_hra"])),
            monthly_special_allowance=Decimal(str(r["monthly_special_allowance"])),
            monthly_da=Decimal(str(r["monthly_da"])),
            is_epf_eligible=bool(r["is_epf_eligible"]),
            epf_wage_cap_applied=bool(r["epf_wage_cap_applied"]),
            vpf_percentage=Decimal(str(r["vpf_percentage"])),
            is_esi_eligible=is_emp_esi,
            pt_state_code=str(r["pt_state_code"]),
            tax_regime=str(r["tax_regime"]).upper(),
            unpaid_leave_days=att_sum.unpaid_leave_days,
            unexcused_absent_days=att_sum.unexcused_absent_days,
            approved_reimbursements=exp_sum.total_approved_reimbursements,
            scheduled_advance_emi=adv_sum.total_scheduled_emi,
            tax_declaration=tax_by_emp.get(eid),
            pt_slabs=pt_slabs or None,
            ytd_gross_earned=Decimal(str(ytd_row["ytd_gross"])) if has_prior_ytd else None,
            ytd_basic_plus_da=Decimal(str(ytd_row["ytd_basic_da"])) if has_prior_ytd else None,
            ytd_hra_received=Decimal(str(ytd_row["ytd_hra"])) if has_prior_ytd else None,
            ytd_pt_paid=Decimal(str(ytd_row["ytd_pt"])) if has_prior_ytd else None,
            ytd_epf_employee=Decimal(str(ytd_row["ytd_epf"])) if has_prior_ytd else None,
            ytd_tds_deducted=Decimal(str(ytd_row["ytd_tds"])) if has_prior_ytd else ZERO,
        )

        bundles.append(
            EmployeeIngestionBundle(
                employee_id=eid,
                employee_name=r["employee_name"],
                email=r["email"],
                engine_input=emp_input,
                reimbursement_ids=exp_sum.reimbursement_ids,
                advance_recoveries=adv_sum.advance_recoveries,
                attendance_summary=att_sum,
                leave_summary=leave_sum,
                expense_summary=exp_sum,
                advance_summary=adv_sum,
                missing_statutory_warnings=warnings,
            )
        )

    return CycleIngestionResult(
        cycle_month=cycle_month,
        cycle_year=cycle_year,
        financial_year=fy_label,
        start_date=start_date,
        end_date=end_date,
        cutoff_date=cutoff_date,
        calendar_summary=cal_summary,
        bundles=bundles,
    )
