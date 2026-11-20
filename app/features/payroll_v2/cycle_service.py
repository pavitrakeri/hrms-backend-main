"""
Payroll Cycle Workflow Orchestrator, State Machine, Line-Item Hold & ESS Service.

State Machine (`015_india_payroll_schema.sql`):
  DRAFT -> (CALCULATING) -> DRAFT (entries computed) -> REVIEW -> APPROVED -> DISBURSED
                                                          |
                                                          +--> REJECTED -> DRAFT (re-calc / edit)

Approval Chain:
  Step 1: hr_admin        (hr / admin)
  Step 2: finance_manager (finance / admin)
  Step 3: cfo             (cfo / admin) -> moves cycle to APPROVED

Line-Item Hold Exception Pattern:
  - Entries may be toggled CLEARED <-> ON_HOLD prior to final CFO approval (DRAFT/REVIEW/REJECTED).
  - All `payroll_cycles` monetary aggregates + `employee_count` strictly sum `WHERE status = 'CLEARED'`.
  - On DISBURSED, ONLY `CLEARED` entries settle `payout_status='PAID'`, settle matched `reimbursements`,
    and reduce `employee_salary_advances` balances. `ON_HOLD` rows remain untouched for OFF_CYCLE runs.
"""

from __future__ import annotations

import calendar
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from fastapi import HTTPException

from app.datamodels.payroll_v2_models import (
    CycleActionStatusResponse,
    CycleApprovalActionRequest,
    CycleCalculateRequest,
    CycleCreateRequest,
    CycleMarkDisbursedRequest,
    EmployeePayslipDetailResponse,
    EmployeePayslipListItem,
    EntryHoldRequest,
    EntryReleaseRequest,
    PayrollCycleApprovalRow,
    PayrollCycleDetailResponse,
    PayrollCycleSummary,
    PayrollEntryRow,
    ProvisionalPreviewResponse,
)
from app.features.payroll_v2.adapters.advance_adapter import ScheduledAdvanceRecovery
from app.features.payroll_v2.adapters.employee_aggregator import (
    EmployeeIngestionBundle,
    fetch_cycle_employee_inputs,
)
from app.features.payroll_v2.engine_india import (
    PayrollCalculationResult,
    calculate_monthly_payroll,
    round_paisa,
)
from app.features.payroll_v2.master_service import (
    get_company_statutory_details,
    get_employee_bank_details,
    validate_uuid,
)
from app.features.payroll_v2.rbac import (
    APPROVAL_CHAIN_STEPS,
    require_approval_step_role,
)

IST = timezone(timedelta(hours=5, minutes=30))
ZERO = Decimal("0.00")
MUTABLE_ENTRY_CYCLE_STATUSES = frozenset({"DRAFT", "REVIEW", "REJECTED"})
RECALCULABLE_CYCLE_STATUSES = frozenset({"DRAFT", "REJECTED"})
SUBMITTABLE_CYCLE_STATUSES = frozenset({"DRAFT", "REJECTED"})


def _iso(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    return str(val)


def _opt_col(r: Any, key: str, default: Any = None) -> Any:
    try:
        return r[key]
    except (KeyError, IndexError):
        return default


def _parse_jsonb(val: Any, default: Any = None) -> dict[str, Any]:
    if val is None:
        return {} if default is None else default
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, dict) else ({} if default is None else default)
        except Exception:
            return {} if default is None else default
    return {} if default is None else default


def _allocate_advance_recoveries(
    scheduled: list[ScheduledAdvanceRecovery],
    actual_total_recovered: Decimal,
) -> list[dict[str, Any]]:
    """
    Distribute capped `advance_recovery_amount` sequentially across active loans
    so loan balance reductions on DISBURSED equal the exact paycheck deduction.
    """
    remaining_budget = round_paisa(actual_total_recovered)
    allocations: list[dict[str, Any]] = []
    for item in scheduled:
        sched_amt = round_paisa(item.scheduled_amount)
        if remaining_budget <= ZERO or sched_amt <= ZERO:
            applied = ZERO
        else:
            applied = min(sched_amt, remaining_budget)
            remaining_budget = round_paisa(remaining_budget - applied)
        if applied > ZERO:
            allocations.append(
                {
                    "advance_id": str(item.advance_id),
                    "scheduled_amount": float(sched_amt),
                    "applied_amount": float(applied),
                }
            )
    return allocations


def _build_enriched_snapshots(
    calc: PayrollCalculationResult,
    bundle: EmployeeIngestionBundle,
) -> tuple[dict[str, Any], dict[str, Any]]:
    earnings_json = dict(calc.earnings_breakdown)
    earnings_json["employee_name"] = bundle.employee_name
    earnings_json["reimbursement_ids"] = [str(rid) for rid in bundle.reimbursement_ids]
    earnings_json["reimbursement_items"] = [
        {
            "id": str(item.reimbursement_id),
            "category": item.category,
            "subcategory": item.subcategory,
            "amount": float(item.amount),
            "expense_date": item.expense_date.isoformat(),
        }
        for item in bundle.expense_summary.items
    ]
    earnings_json["contractual_components"] = {
        "basic": float(bundle.engine_input.monthly_basic_pay),
        "hra": float(bundle.engine_input.monthly_hra),
        "special_allowance": float(bundle.engine_input.monthly_special_allowance),
        "da": float(bundle.engine_input.monthly_da),
    }

    allocated_advances = _allocate_advance_recoveries(
        bundle.advance_recoveries,
        calc.advance_recovery_amount,
    )
    deductions_json = dict(calc.deductions_breakdown)
    deductions_json["advance_recoveries"] = allocated_advances
    deductions_json["edli_wages"] = float(calc.edli_wages)
    deductions_json["tax_breakdown"] = calc.tax_breakdown.model_dump(mode="json")
    deductions_json["warnings"] = list(bundle.missing_statutory_warnings)

    return earnings_json, deductions_json


def _row_to_cycle_summary(
    r: Any, *, cleared_count: Optional[int] = None, on_hold_count: Optional[int] = None
) -> PayrollCycleSummary:
    emp_count = int(r["employee_count"])
    c_cnt = cleared_count if cleared_count is not None else int(_opt_col(r, "cleared_count", emp_count) or 0)
    h_cnt = on_hold_count if on_hold_count is not None else int(_opt_col(r, "on_hold_count", 0) or 0)
    return PayrollCycleSummary(
        id=str(r["id"]),
        cycle_name=r["cycle_name"],
        cycle_month=int(r["cycle_month"]),
        cycle_year=int(r["cycle_year"]),
        start_date=_iso(r["start_date"]) or "",
        end_date=_iso(r["end_date"]) or "",
        cutoff_date=_iso(r["cutoff_date"]) or "",
        cycle_type=r["cycle_type"],
        reference_cycle_id=str(r["reference_cycle_id"]) if r["reference_cycle_id"] else None,
        status=r["status"],
        total_gross=float(r["total_gross"]),
        total_epf_employee=float(r["total_epf_employee"]),
        total_eps_employer=float(r["total_eps_employer"]),
        total_epf_employer=float(r["total_epf_employer"]),
        total_esi_employee=float(r["total_esi_employee"]),
        total_esi_employer=float(r["total_esi_employer"]),
        total_pt=float(r["total_pt"]),
        total_tds=float(r["total_tds"]),
        total_reimbursements=float(r["total_reimbursements"]),
        total_advance_deductions=float(r["total_advance_deductions"]),
        total_net_payable=float(r["total_net_payable"]),
        total_company_cost=float(r["total_company_cost"]),
        employee_count=emp_count,
        cleared_count=c_cnt,
        on_hold_count=h_cnt,
        bank_batch_reference=r["bank_batch_reference"],
        bank_export_url=r["bank_export_url"],
        ecr_export_url=r["ecr_export_url"],
        created_by=str(r["created_by"]) if r["created_by"] else None,
        approved_by=str(r["approved_by"]) if r["approved_by"] else None,
        disbursed_at=_iso(r["disbursed_at"]),
        created_at=_iso(r["created_at"]),
        updated_at=_iso(r["updated_at"]),
    )


def _row_to_entry(r: Any) -> PayrollEntryRow:
    return PayrollEntryRow(
        id=str(r["id"]),
        cycle_id=str(r["cycle_id"]),
        employee_id=str(r["employee_id"]),
        employee_name=_opt_col(r, "employee_name"),
        employee_email=_opt_col(r, "employee_email"),
        department=_opt_col(r, "department"),
        designation=_opt_col(r, "designation"),
        pan_number=_opt_col(r, "pan_number"),
        uan_number=_opt_col(r, "uan_number"),
        status=r["status"],
        hold_reason=r["hold_reason"],
        total_calendar_days=int(r["total_calendar_days"]),
        days_worked=float(r["days_worked"]),
        unpaid_leave_days=float(r["unpaid_leave_days"]),
        unexcused_absent_days=float(r["unexcused_absent_days"]),
        ncp_days=float(r["ncp_days"]),
        earned_basic=float(r["earned_basic"]),
        earned_hra=float(r["earned_hra"]),
        earned_special_allowance=float(r["earned_special_allowance"]),
        earned_da=float(r["earned_da"]),
        gross_earnings=float(r["gross_earnings"]),
        epf_wages=float(r["epf_wages"]),
        eps_wages=float(r["eps_wages"]),
        esi_wages=float(r["esi_wages"]),
        epf_employee_share=float(r["epf_employee_share"]),
        eps_employer_share=float(r["eps_employer_share"]),
        epf_employer_share=float(r["epf_employer_share"]),
        edli_employer_share=float(r["edli_employer_share"]),
        epf_admin_charges=float(r["epf_admin_charges"]),
        esi_employee_share=float(r["esi_employee_share"]),
        esi_employer_share=float(r["esi_employer_share"]),
        professional_tax=float(r["professional_tax"]),
        tds_amount=float(r["tds_amount"]),
        reimbursements_amount=float(r["reimbursements_amount"]),
        advance_recovery_amount=float(r["advance_recovery_amount"]),
        net_payable=float(r["net_payable"]),
        gratuity_accrual_monthly=float(r["gratuity_accrual_monthly"]),
        total_employer_cost=float(r["total_employer_cost"]),
        earnings_breakdown=_parse_jsonb(r["earnings_breakdown"]),
        deductions_breakdown=_parse_jsonb(r["deductions_breakdown"]),
        payslip_pdf_url=r["payslip_pdf_url"],
        bank_utr_number=r["bank_utr_number"],
        payout_status=r["payout_status"],
        bank_verified=bool(r["bank_verified"]) if ("bank_verified" in r and r["bank_verified"] is not None) else False,
        bank_response_code=_opt_col(r, "bank_response_code"),
        bank_response_message=_opt_col(r, "bank_response_message"),
        bank_transaction_date=_iso(_opt_col(r, "bank_transaction_date")),
        reconciled_at=_iso(_opt_col(r, "reconciled_at")),
        remediation_status=_opt_col(r, "remediation_status", "NONE"),
        remediation_notes=_opt_col(r, "remediation_notes"),
        paid_at=_iso(r["paid_at"]),
        created_at=_iso(r["created_at"]),
        updated_at=_iso(r["updated_at"]),
    )


def _row_to_approval(r: Any) -> PayrollCycleApprovalRow:
    return PayrollCycleApprovalRow(
        id=str(r["id"]),
        cycle_id=str(r["cycle_id"]),
        step_order=int(r["step_order"]),
        role_required=r["role_required"],
        approver_id=str(r["approver_id"]) if r["approver_id"] else None,
        approver_name=_opt_col(r, "approver_name"),
        status=r["status"],
        comments=r["comments"],
        decided_at=_iso(r["decided_at"]),
        created_at=_iso(r["created_at"]),
    )


async def recompute_cycle_aggregates(conn, cycle_id: str) -> PayrollCycleSummary:
    """
    Recompute all monetary totals and `employee_count` on `payroll_cycles`
    strictly aggregating `payroll_entries` WHERE `status = 'CLEARED'`.
    """
    cid = validate_uuid(cycle_id, "cycle_id")
    agg = await conn.fetchrow(
        """
        SELECT
            COALESCE(SUM(gross_earnings) FILTER (WHERE status = 'CLEARED'), 0) AS total_gross,
            COALESCE(SUM(epf_employee_share) FILTER (WHERE status = 'CLEARED'), 0) AS total_epf_employee,
            COALESCE(SUM(eps_employer_share) FILTER (WHERE status = 'CLEARED'), 0) AS total_eps_employer,
            COALESCE(SUM(epf_employer_share) FILTER (WHERE status = 'CLEARED'), 0) AS total_epf_employer,
            COALESCE(SUM(esi_employee_share) FILTER (WHERE status = 'CLEARED'), 0) AS total_esi_employee,
            COALESCE(SUM(esi_employer_share) FILTER (WHERE status = 'CLEARED'), 0) AS total_esi_employer,
            COALESCE(SUM(professional_tax) FILTER (WHERE status = 'CLEARED'), 0) AS total_pt,
            COALESCE(SUM(tds_amount) FILTER (WHERE status = 'CLEARED'), 0) AS total_tds,
            COALESCE(SUM(reimbursements_amount) FILTER (WHERE status = 'CLEARED'), 0) AS total_reimbursements,
            COALESCE(SUM(advance_recovery_amount) FILTER (WHERE status = 'CLEARED'), 0) AS total_advance_deductions,
            COALESCE(SUM(net_payable) FILTER (WHERE status = 'CLEARED'), 0) AS total_net_payable,
            COALESCE(SUM(total_employer_cost) FILTER (WHERE status = 'CLEARED'), 0) AS total_company_cost,
            COUNT(*) FILTER (WHERE status = 'CLEARED')::int AS employee_count,
            COUNT(*) FILTER (WHERE status = 'ON_HOLD')::int AS on_hold_count
        FROM payroll_entries
        WHERE cycle_id = $1::uuid;
        """,
        cid,
    )

    updated = await conn.fetchrow(
        """
        UPDATE payroll_cycles
        SET total_gross = $2,
            total_epf_employee = $3,
            total_eps_employer = $4,
            total_epf_employer = $5,
            total_esi_employee = $6,
            total_esi_employer = $7,
            total_pt = $8,
            total_tds = $9,
            total_reimbursements = $10,
            total_advance_deductions = $11,
            total_net_payable = $12,
            total_company_cost = $13,
            employee_count = $14,
            updated_at = now()
        WHERE id = $1::uuid
        RETURNING *;
        """,
        cid,
        agg["total_gross"],
        agg["total_epf_employee"],
        agg["total_eps_employer"],
        agg["total_epf_employer"],
        agg["total_esi_employee"],
        agg["total_esi_employer"],
        agg["total_pt"],
        agg["total_tds"],
        agg["total_reimbursements"],
        agg["total_advance_deductions"],
        agg["total_net_payable"],
        agg["total_company_cost"],
        int(agg["employee_count"]),
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Payroll cycle not found.")

    return _row_to_cycle_summary(
        updated,
        cleared_count=int(agg["employee_count"]),
        on_hold_count=int(agg["on_hold_count"]),
    )


async def _upsert_payroll_entry(
    conn,
    *,
    cycle_id: str,
    bundle: EmployeeIngestionBundle,
    calc: PayrollCalculationResult,
    status: str,
    hold_reason: Optional[str],
) -> None:
    earnings_json, deductions_json = _build_enriched_snapshots(calc, bundle)

    await conn.execute(
        """
        INSERT INTO payroll_entries (
            cycle_id, employee_id, status, hold_reason,
            total_calendar_days, days_worked, unpaid_leave_days, unexcused_absent_days, ncp_days,
            earned_basic, earned_hra, earned_special_allowance, earned_da, gross_earnings,
            epf_wages, eps_wages, esi_wages,
            epf_employee_share, eps_employer_share, epf_employer_share,
            edli_employer_share, epf_admin_charges,
            esi_employee_share, esi_employer_share, professional_tax, tds_amount,
            reimbursements_amount, advance_recovery_amount, net_payable,
            gratuity_accrual_monthly, total_employer_cost,
            earnings_breakdown, deductions_breakdown,
            payout_status
        )
        VALUES (
            $1::uuid, $2::uuid, $3, $4,
            $5, $6, $7, $8, $9,
            $10, $11, $12, $13, $14,
            $15, $16, $17,
            $18, $19, $20,
            $21, $22,
            $23, $24, $25, $26,
            $27, $28, $29,
            $30, $31,
            $32::jsonb, $33::jsonb,
            'PENDING'
        )
        ON CONFLICT (cycle_id, employee_id)
        DO UPDATE SET
            status = EXCLUDED.status,
            hold_reason = EXCLUDED.hold_reason,
            total_calendar_days = EXCLUDED.total_calendar_days,
            days_worked = EXCLUDED.days_worked,
            unpaid_leave_days = EXCLUDED.unpaid_leave_days,
            unexcused_absent_days = EXCLUDED.unexcused_absent_days,
            ncp_days = EXCLUDED.ncp_days,
            earned_basic = EXCLUDED.earned_basic,
            earned_hra = EXCLUDED.earned_hra,
            earned_special_allowance = EXCLUDED.earned_special_allowance,
            earned_da = EXCLUDED.earned_da,
            gross_earnings = EXCLUDED.gross_earnings,
            epf_wages = EXCLUDED.epf_wages,
            eps_wages = EXCLUDED.eps_wages,
            esi_wages = EXCLUDED.esi_wages,
            epf_employee_share = EXCLUDED.epf_employee_share,
            eps_employer_share = EXCLUDED.eps_employer_share,
            epf_employer_share = EXCLUDED.epf_employer_share,
            edli_employer_share = EXCLUDED.edli_employer_share,
            epf_admin_charges = EXCLUDED.epf_admin_charges,
            esi_employee_share = EXCLUDED.esi_employee_share,
            esi_employer_share = EXCLUDED.esi_employer_share,
            professional_tax = EXCLUDED.professional_tax,
            tds_amount = EXCLUDED.tds_amount,
            reimbursements_amount = EXCLUDED.reimbursements_amount,
            advance_recovery_amount = EXCLUDED.advance_recovery_amount,
            net_payable = EXCLUDED.net_payable,
            gratuity_accrual_monthly = EXCLUDED.gratuity_accrual_monthly,
            total_employer_cost = EXCLUDED.total_employer_cost,
            earnings_breakdown = EXCLUDED.earnings_breakdown,
            deductions_breakdown = EXCLUDED.deductions_breakdown,
            updated_at = now();
        """,
        cycle_id,
        bundle.employee_id,
        status,
        hold_reason,
        calc.total_calendar_days,
        calc.days_worked,
        calc.unpaid_leave_days,
        calc.unexcused_absent_days,
        calc.ncp_days,
        calc.earned_basic,
        calc.earned_hra,
        calc.earned_special_allowance,
        calc.earned_da,
        calc.gross_earnings,
        calc.epf_wages,
        calc.eps_wages,
        calc.esi_wages,
        calc.epf_employee_share,
        calc.eps_employer_share,
        calc.epf_employer_share,
        calc.edli_employer_share,
        calc.epf_admin_charges,
        calc.esi_employee_share,
        calc.esi_employer_share,
        calc.professional_tax,
        calc.tds_amount,
        calc.reimbursements_amount,
        calc.advance_recovery_amount,
        calc.net_payable,
        calc.gratuity_accrual_monthly,
        calc.total_employer_cost,
        json.dumps(earnings_json),
        json.dumps(deductions_json),
    )


# ---------------------------------------------------------------------------
# Cycle CRUD & Detail Queries
# ---------------------------------------------------------------------------
async def list_cycles(
    conn,
    *,
    cycle_year: Optional[int] = None,
    status: Optional[str] = None,
) -> list[PayrollCycleSummary]:
    conditions: list[str] = []
    params: list[Any] = []
    idx = 1

    if cycle_year is not None:
        conditions.append(f"pc.cycle_year = ${idx}")
        params.append(cycle_year)
        idx += 1
    if status:
        conditions.append(f"pc.status = ${idx}")
        params.append(status.strip().upper())
        idx += 1

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = await conn.fetch(
        f"""
        SELECT pc.*,
               COALESCE(counts.cleared_count, 0) AS cleared_count,
               COALESCE(counts.on_hold_count, 0) AS on_hold_count
        FROM payroll_cycles pc
        LEFT JOIN (
            SELECT cycle_id,
                   COUNT(*) FILTER (WHERE status = 'CLEARED')::int AS cleared_count,
                   COUNT(*) FILTER (WHERE status = 'ON_HOLD')::int AS on_hold_count
            FROM payroll_entries
            GROUP BY cycle_id
        ) counts ON counts.cycle_id = pc.id
        {where}
        ORDER BY pc.cycle_year DESC, pc.cycle_month DESC, pc.created_at DESC;
        """,
        *params,
    )
    return [_row_to_cycle_summary(r) for r in rows]


async def create_cycle(conn, user: dict[str, Any], req: CycleCreateRequest) -> PayrollCycleSummary:
    ref_uuid: Optional[str] = None
    if req.reference_cycle_id:
        ref_uuid = validate_uuid(req.reference_cycle_id, "reference_cycle_id")
        ref_exists = await conn.fetchval(
            "SELECT 1 FROM payroll_cycles WHERE id = $1::uuid;", ref_uuid
        )
        if not ref_exists:
            raise HTTPException(
                status_code=404, detail="Reference payroll cycle not found."
            )

    if req.cycle_type == "REGULAR":
        dup = await conn.fetchval(
            """
            SELECT 1 FROM payroll_cycles
            WHERE cycle_year = $1 AND cycle_month = $2 AND cycle_type = 'REGULAR';
            """,
            req.cycle_year,
            req.cycle_month,
        )
        if dup:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"A REGULAR payroll cycle already exists for "
                    f"{req.cycle_month:02d}/{req.cycle_year}."
                ),
            )

    creator_uuid = validate_uuid(user["id"], "created_by")
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO payroll_cycles (
                cycle_name, cycle_month, cycle_year,
                start_date, end_date, cutoff_date,
                cycle_type, reference_cycle_id, status, created_by
            )
            VALUES (
                $1, $2, $3,
                $4, $5, $6,
                $7, $8::uuid, 'DRAFT', $9::uuid
            )
            RETURNING *;
            """,
            req.cycle_name,
            req.cycle_month,
            req.cycle_year,
            req.start_date,
            req.end_date,
            req.cutoff_date,
            req.cycle_type,
            ref_uuid,
            creator_uuid,
        )
    except Exception as exc:
        if "unique" in str(exc).lower() or "23505" in str(exc):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"A REGULAR payroll cycle already exists for "
                    f"{req.cycle_month:02d}/{req.cycle_year}."
                ),
            ) from exc
        raise

    return _row_to_cycle_summary(row, cleared_count=0, on_hold_count=0)


async def get_cycle_detail(conn, cycle_id: str) -> PayrollCycleDetailResponse:
    cid = validate_uuid(cycle_id, "cycle_id")
    cycle_row = await conn.fetchrow(
        "SELECT * FROM payroll_cycles WHERE id = $1::uuid;", cid
    )
    if not cycle_row:
        raise HTTPException(status_code=404, detail="Payroll cycle not found.")

    entry_rows = await conn.fetch(
        """
        SELECT pe.*,
               u.full_name AS employee_name,
               u.email AS employee_email,
               d.name AS department,
               u.designation,
               u.pan_number,
               u.uan_number
        FROM payroll_entries pe
        JOIN users u ON u.id = pe.employee_id
        LEFT JOIN departments d ON d.id = u.department_id
        WHERE pe.cycle_id = $1::uuid
        ORDER BY CASE WHEN pe.status = 'ON_HOLD' THEN 0 ELSE 1 END,
                 u.full_name ASC;
        """,
        cid,
    )

    approval_rows = await conn.fetch(
        """
        SELECT pca.*, u.full_name AS approver_name
        FROM payroll_cycle_approvals pca
        LEFT JOIN users u ON u.id = pca.approver_id
        WHERE pca.cycle_id = $1::uuid
        ORDER BY pca.step_order ASC, pca.created_at ASC;
        """,
        cid,
    )

    entries = [_row_to_entry(r) for r in entry_rows]
    cleared_count = sum(1 for e in entries if e.status == "CLEARED")
    on_hold_count = sum(1 for e in entries if e.status == "ON_HOLD")

    return PayrollCycleDetailResponse(
        cycle=_row_to_cycle_summary(
            cycle_row,
            cleared_count=cleared_count,
            on_hold_count=on_hold_count,
        ),
        entries=entries,
        approvals=[_row_to_approval(a) for a in approval_rows],
    )


# ---------------------------------------------------------------------------
# Batch Calculation & Single-Record Recalculation
# ---------------------------------------------------------------------------
async def calculate_cycle(
    conn,
    cycle_id: str,
    req: Optional[CycleCalculateRequest] = None,
) -> PayrollCycleDetailResponse:
    cid = validate_uuid(cycle_id, "cycle_id")
    opts = req or CycleCalculateRequest()

    async with conn.transaction():
        cycle = await conn.fetchrow(
            "SELECT * FROM payroll_cycles WHERE id = $1::uuid FOR UPDATE;", cid
        )
        if not cycle:
            raise HTTPException(status_code=404, detail="Payroll cycle not found.")
        if cycle["status"] not in RECALCULABLE_CYCLE_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot batch-calculate cycle in '{cycle['status']}' status. "
                    f"Allowed statuses: DRAFT, REJECTED."
                ),
            )

        was_rejected = cycle["status"] == "REJECTED"
        await conn.execute(
            "UPDATE payroll_cycles SET status = 'CALCULATING', updated_at = now() WHERE id = $1::uuid;",
            cid,
        )

        if was_rejected:
            await conn.execute(
                "DELETE FROM payroll_cycle_approvals WHERE cycle_id = $1::uuid;", cid
            )

        existing_rows = await conn.fetch(
            "SELECT employee_id::text AS employee_id, status, hold_reason FROM payroll_entries WHERE cycle_id = $1::uuid;",
            cid,
        )
        held_map: dict[str, Optional[str]] = {
            r["employee_id"]: r["hold_reason"]
            for r in existing_rows
            if r["status"] == "ON_HOLD"
        }

        target_emp_ids: Optional[list[str]] = None
        if opts.employee_ids is not None:
            target_emp_ids = [validate_uuid(eid, "employee_ids") for eid in opts.employee_ids]

        ingestion = await fetch_cycle_employee_inputs(
            conn,
            cycle_month=int(cycle["cycle_month"]),
            cycle_year=int(cycle["cycle_year"]),
            start_date=cycle["start_date"],
            end_date=cycle["end_date"],
            cutoff_date=cycle["cutoff_date"],
            employee_ids=target_emp_ids,
            reference_cycle_id=(
                str(cycle["reference_cycle_id"]) if cycle["reference_cycle_id"] else None
            ),
            proration_divisor=opts.proration_divisor,
            as_of_date=opts.as_of_date,
            strict_attendance=opts.strict_attendance,
        )

        if not ingestion.bundles:
            raise HTTPException(
                status_code=400,
                detail=(
                    "No eligible active employees found for calculation "
                    "(verify active salary structures or held entries on reference cycle)."
                ),
            )

        calculated_emp_ids: list[str] = []
        for bundle in ingestion.bundles:
            calc = calculate_monthly_payroll(bundle.engine_input)
            if opts.preserve_holds and bundle.employee_id in held_map:
                entry_status = "ON_HOLD"
                entry_hold_reason = held_map[bundle.employee_id]
            else:
                entry_status = "CLEARED"
                entry_hold_reason = None

            await _upsert_payroll_entry(
                conn,
                cycle_id=cid,
                bundle=bundle,
                calc=calc,
                status=entry_status,
                hold_reason=entry_hold_reason,
            )
            calculated_emp_ids.append(bundle.employee_id)

        if opts.employee_ids is None:
            await conn.execute(
                """
                DELETE FROM payroll_entries
                WHERE cycle_id = $1::uuid
                  AND NOT (employee_id = ANY($2::uuid[]));
                """,
                cid,
                calculated_emp_ids,
            )

        await recompute_cycle_aggregates(conn, cid)
        await conn.execute(
            "UPDATE payroll_cycles SET status = 'DRAFT', updated_at = now() WHERE id = $1::uuid;",
            cid,
        )
        try:
            from app.features.payroll_v2.statutory_remittance import init_statutory_remittances_for_cycle
            await init_statutory_remittances_for_cycle(conn, str(cid))
        except Exception:
            pass

    return await get_cycle_detail(conn, cid)


# ---------------------------------------------------------------------------
# Line-Item Hold, Release & Single-Record Recalculation
# ---------------------------------------------------------------------------
async def hold_entry(
    conn, cycle_id: str, record_id: str, req: EntryHoldRequest
) -> PayrollCycleDetailResponse:
    cid = validate_uuid(cycle_id, "cycle_id")
    rid = validate_uuid(record_id, "record_id")

    async with conn.transaction():
        cycle = await conn.fetchrow(
            "SELECT id, status FROM payroll_cycles WHERE id = $1::uuid FOR UPDATE;", cid
        )
        if not cycle:
            raise HTTPException(status_code=404, detail="Payroll cycle not found.")
        if cycle["status"] not in MUTABLE_ENTRY_CYCLE_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=f"Cannot place record ON_HOLD while cycle is '{cycle['status']}'.",
            )

        updated = await conn.fetchrow(
            """
            UPDATE payroll_entries
            SET status = 'ON_HOLD',
                hold_reason = $3,
                updated_at = now()
            WHERE id = $1::uuid AND cycle_id = $2::uuid
            RETURNING id;
            """,
            rid,
            cid,
            req.reason,
        )
        if not updated:
            raise HTTPException(
                status_code=404, detail="Payroll entry not found in this cycle."
            )

        await recompute_cycle_aggregates(conn, cid)

    return await get_cycle_detail(conn, cid)


async def release_entry(
    conn, cycle_id: str, record_id: str, req: Optional[EntryReleaseRequest] = None
) -> PayrollCycleDetailResponse:
    cid = validate_uuid(cycle_id, "cycle_id")
    rid = validate_uuid(record_id, "record_id")

    async with conn.transaction():
        cycle = await conn.fetchrow(
            "SELECT id, status FROM payroll_cycles WHERE id = $1::uuid FOR UPDATE;", cid
        )
        if not cycle:
            raise HTTPException(status_code=404, detail="Payroll cycle not found.")
        if cycle["status"] not in MUTABLE_ENTRY_CYCLE_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=f"Cannot release held record while cycle is '{cycle['status']}'.",
            )

        release_note = req.note.strip() if req and req.note else None
        updated = await conn.fetchrow(
            """
            UPDATE payroll_entries
            SET status = 'CLEARED',
                hold_reason = $3,
                updated_at = now()
            WHERE id = $1::uuid AND cycle_id = $2::uuid
            RETURNING id;
            """,
            rid,
            cid,
            release_note,
        )
        if not updated:
            raise HTTPException(
                status_code=404, detail="Payroll entry not found in this cycle."
            )

        await recompute_cycle_aggregates(conn, cid)

    return await get_cycle_detail(conn, cid)


async def recalculate_single_entry(
    conn,
    cycle_id: str,
    record_id: str,
    *,
    strict_attendance: bool = True,
) -> PayrollCycleDetailResponse:
    cid = validate_uuid(cycle_id, "cycle_id")
    rid = validate_uuid(record_id, "record_id")

    async with conn.transaction():
        cycle = await conn.fetchrow(
            "SELECT * FROM payroll_cycles WHERE id = $1::uuid FOR UPDATE;", cid
        )
        if not cycle:
            raise HTTPException(status_code=404, detail="Payroll cycle not found.")
        if cycle["status"] not in MUTABLE_ENTRY_CYCLE_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=f"Cannot recalculate entry while cycle is '{cycle['status']}'.",
            )

        entry = await conn.fetchrow(
            """
            SELECT id, employee_id::text AS employee_id, status, hold_reason
            FROM payroll_entries
            WHERE id = $1::uuid AND cycle_id = $2::uuid
            FOR UPDATE;
            """,
            rid,
            cid,
        )
        if not entry:
            raise HTTPException(
                status_code=404, detail="Payroll entry not found in this cycle."
            )

        ingestion = await fetch_cycle_employee_inputs(
            conn,
            cycle_month=int(cycle["cycle_month"]),
            cycle_year=int(cycle["cycle_year"]),
            start_date=cycle["start_date"],
            end_date=cycle["end_date"],
            cutoff_date=cycle["cutoff_date"],
            employee_ids=[entry["employee_id"]],
            reference_cycle_id=None,
            strict_attendance=strict_attendance,
        )
        if not ingestion.bundles:
            raise HTTPException(
                status_code=400,
                detail="Employee is inactive or lacks an active salary structure.",
            )

        bundle = ingestion.bundles[0]
        calc = calculate_monthly_payroll(bundle.engine_input)
        await _upsert_payroll_entry(
            conn,
            cycle_id=cid,
            bundle=bundle,
            calc=calc,
            status=entry["status"],
            hold_reason=entry["hold_reason"],
        )
        await recompute_cycle_aggregates(conn, cid)

    return await get_cycle_detail(conn, cid)


# ---------------------------------------------------------------------------
# 3-Tier Sequential Approval Workflow & Final Disbursement
# ---------------------------------------------------------------------------
async def submit_cycle_for_review(conn, cycle_id: str) -> PayrollCycleDetailResponse:
    cid = validate_uuid(cycle_id, "cycle_id")

    async with conn.transaction():
        cycle = await conn.fetchrow(
            "SELECT id, status FROM payroll_cycles WHERE id = $1::uuid FOR UPDATE;", cid
        )
        if not cycle:
            raise HTTPException(status_code=404, detail="Payroll cycle not found.")
        if cycle["status"] not in SUBMITTABLE_CYCLE_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=f"Cannot submit cycle in '{cycle['status']}' status.",
            )

        summary = await recompute_cycle_aggregates(conn, cid)
        if summary.cleared_count <= 0:
            raise HTTPException(
                status_code=400,
                detail="Cannot submit a cycle with 0 CLEARED employee entries.",
            )

        await conn.execute(
            "DELETE FROM payroll_cycle_approvals WHERE cycle_id = $1::uuid;", cid
        )
        for step_order, role_req in APPROVAL_CHAIN_STEPS:
            await conn.execute(
                """
                INSERT INTO payroll_cycle_approvals (
                    cycle_id, step_order, role_required, status
                )
                VALUES ($1::uuid, $2, $3, 'PENDING');
                """,
                cid,
                step_order,
                role_req,
            )

        await conn.execute(
            "UPDATE payroll_cycles SET status = 'REVIEW', updated_at = now() WHERE id = $1::uuid;",
            cid,
        )

    return await get_cycle_detail(conn, cid)


async def action_cycle_approval(
    conn,
    user: dict[str, Any],
    cycle_id: str,
    req: CycleApprovalActionRequest,
) -> PayrollCycleDetailResponse:
    cid = validate_uuid(cycle_id, "cycle_id")
    approver_uuid = validate_uuid(user["id"], "approver_id")

    async with conn.transaction():
        cycle = await conn.fetchrow(
            "SELECT id, status FROM payroll_cycles WHERE id = $1::uuid FOR UPDATE;", cid
        )
        if not cycle:
            raise HTTPException(status_code=404, detail="Payroll cycle not found.")
        if cycle["status"] != "REVIEW":
            raise HTTPException(
                status_code=409,
                detail=f"Cycle must be in 'REVIEW' status for approval actions (current: '{cycle['status']}').",
            )

        approvals = await conn.fetch(
            """
            SELECT id, step_order, role_required, status
            FROM payroll_cycle_approvals
            WHERE cycle_id = $1::uuid
            ORDER BY step_order ASC
            FOR UPDATE;
            """,
            cid,
        )
        if not approvals:
            raise HTTPException(
                status_code=409,
                detail="No approval chain initialized for this cycle.",
            )

        pending_step = next((a for a in approvals if a["status"] == "PENDING"), None)
        if not pending_step:
            raise HTTPException(
                status_code=409, detail="All approval steps have already been decided."
            )

        require_approval_step_role(
            user,
            step_order=int(pending_step["step_order"]),
            role_required=str(pending_step["role_required"]),
        )

        await conn.execute(
            """
            UPDATE payroll_cycle_approvals
            SET approver_id = $2::uuid,
                status = $3,
                comments = $4,
                decided_at = now()
            WHERE id = $1::uuid;
            """,
            pending_step["id"],
            approver_uuid,
            req.decision,
            req.comments,
        )

        if req.decision == "REJECTED":
            await conn.execute(
                "UPDATE payroll_cycles SET status = 'REJECTED', updated_at = now() WHERE id = $1::uuid;",
                cid,
            )
        else:
            is_final_step = all(
                (a["id"] == pending_step["id"]) or (a["status"] == "APPROVED")
                for a in approvals
            )
            if is_final_step:
                await conn.execute(
                    """
                    UPDATE payroll_cycles
                    SET status = 'APPROVED',
                        approved_by = $2::uuid,
                        updated_at = now()
                    WHERE id = $1::uuid;
                    """,
                    cid,
                    approver_uuid,
                )

    return await get_cycle_detail(conn, cid)


async def mark_cycle_disbursed(
    conn,
    cycle_id: str,
    req: CycleMarkDisbursedRequest,
) -> CycleActionStatusResponse:
    """
    Atomically settle an APPROVED payroll cycle:
      1. Mark ONLY `status = 'CLEARED'` payroll_entries as `PAID` with UTR & `paid_at`.
      2. Leave `ON_HOLD` entries untouched (`payout_status = 'PENDING'`, `paid_at = NULL`).
      3. Settle `reimbursements` referenced by CLEARED entries (`status = 'paid', paid_at = now()`).
      4. Reduce `employee_salary_advances.remaining_balance`, increment `paid_installments`,
         and mark `'COMPLETED'` once remaining_balance hits 0 — strictly for CLEARED entries.
      5. Mark cycle `DISBURSED` with `bank_batch_reference` and `disbursed_at`.
    """
    cid = validate_uuid(cycle_id, "cycle_id")
    utr_map = req.utr_numbers or {}

    async with conn.transaction():
        cycle = await conn.fetchrow(
            "SELECT * FROM payroll_cycles WHERE id = $1::uuid FOR UPDATE;", cid
        )
        if not cycle:
            raise HTTPException(status_code=404, detail="Payroll cycle not found.")
        if cycle["status"] != "APPROVED":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Only an 'APPROVED' cycle can be disbursed "
                    f"(current status: '{cycle['status']}')."
                ),
            )

        cleared_entries = await conn.fetch(
            """
            SELECT id, employee_id::text AS employee_id, net_payable, earnings_breakdown, deductions_breakdown
            FROM payroll_entries
            WHERE cycle_id = $1::uuid
              AND status = 'CLEARED'
            FOR UPDATE;
            """,
            cid,
        )
        if not cleared_entries:
            raise HTTPException(
                status_code=400, detail="No CLEARED entries exist to disburse."
            )

        reimbursement_ids_to_settle: set[str] = set()
        advance_applied_totals: dict[str, Decimal] = {}
        advance_installments_count: dict[str, int] = {}

        for entry in cleared_entries:
            entry_id_str = str(entry["id"])
            emp_id_str = str(entry["employee_id"])
            custom_utr = utr_map.get(entry_id_str) or utr_map.get(emp_id_str)
            is_bank_confirmed = bool(custom_utr and custom_utr.strip())
            # For backward compatibility, fallback to batch ref if custom UTR not supplied,
            # but mark bank_verified = False so manual confirmation is not misrepresented as bank-confirmed.
            utr_to_store = custom_utr.strip() if is_bank_confirmed else req.bank_batch_reference.strip()

            await conn.execute(
                """
                UPDATE payroll_entries
                SET payout_status = 'PAID',
                    bank_utr_number = $2,
                    bank_verified = $3,
                    paid_at = now(),
                    updated_at = now()
                WHERE id = $1::uuid;
                """,
                entry["id"],
                utr_to_store,
                is_bank_confirmed,
            )

            e_json = _parse_jsonb(entry["earnings_breakdown"])
            for rid in e_json.get("reimbursement_ids") or []:
                if rid:
                    reimbursement_ids_to_settle.add(str(rid))

            d_json = _parse_jsonb(entry["deductions_breakdown"])
            for alloc in d_json.get("advance_recoveries") or []:
                adv_id = alloc.get("advance_id")
                applied = round_paisa(Decimal(str(alloc.get("applied_amount", "0"))))
                if adv_id and applied > ZERO:
                    adv_key = str(adv_id)
                    advance_applied_totals[adv_key] = round_paisa(
                        advance_applied_totals.get(adv_key, ZERO) + applied
                    )
                    advance_installments_count[adv_key] = (
                        advance_installments_count.get(adv_key, 0) + 1
                    )

        # Settle reimbursements ONLY for CLEARED staff
        if reimbursement_ids_to_settle:
            await conn.execute(
                """
                UPDATE reimbursements
                SET status = 'paid',
                    paid_at = now()
                WHERE id = ANY($1::uuid[])
                  AND status = 'approved'
                  AND paid_at IS NULL;
                """,
                list(reimbursement_ids_to_settle),
            )

        # Reduce advance balances ONLY for CLEARED staff
        if advance_applied_totals:
            adv_rows = await conn.fetch(
                """
                SELECT id, remaining_balance, paid_installments, status
                FROM employee_salary_advances
                WHERE id = ANY($1::uuid[])
                FOR UPDATE;
                """,
                list(advance_applied_totals.keys()),
            )
            for adv in adv_rows:
                adv_id_str = str(adv["id"])
                applied = advance_applied_totals.get(adv_id_str, ZERO)
                inst_inc = advance_installments_count.get(adv_id_str, 1)
                curr_bal = round_paisa(Decimal(str(adv["remaining_balance"])))
                new_bal = max(ZERO, round_paisa(curr_bal - applied))
                new_paid_inst = int(adv["paid_installments"]) + inst_inc
                new_status = "COMPLETED" if new_bal <= ZERO else adv["status"]

                await conn.execute(
                    """
                    UPDATE employee_salary_advances
                    SET remaining_balance = $2,
                        paid_installments = $3,
                        status = $4,
                        updated_at = now()
                    WHERE id = $1::uuid;
                    """,
                    adv["id"],
                    new_bal,
                    new_paid_inst,
                    new_status,
                )

        await conn.execute(
            """
            UPDATE payroll_cycles
            SET status = 'DISBURSED',
                bank_batch_reference = $2,
                disbursed_at = now(),
                updated_at = now()
            WHERE id = $1::uuid;
            """,
            cid,
            req.bank_batch_reference,
        )

        batch_total = sum(Decimal(str(r["net_payable"])) for r in cleared_entries)
        await conn.execute(
            """
            INSERT INTO payroll_payment_batches (
                cycle_id, batch_reference, bank_name, payment_mode,
                total_beneficiaries, total_amount, status,
                submission_timestamp, created_at
            )
            VALUES (
                $1::uuid, $2, 'Corporate Bank', 'NEFT_RTGS',
                $3, $4, 'PROCESSED', now(), now()
            )
            ON CONFLICT (batch_reference) DO UPDATE
            SET status = 'PROCESSED', updated_at = now();
            """,
            cid,
            req.bank_batch_reference.strip(),
            len(cleared_entries),
            round_paisa(batch_total),
        )

        await conn.execute(
            """
            INSERT INTO payroll_audit_logs (
                cycle_id, action, entity_type, entity_id, new_state, comments
            )
            VALUES (
                $1::uuid, 'CYCLE_MARKED_DISBURSED', 'payroll_cycles', $1::text,
                $2::jsonb, $3
            );
            """,
            cid,
            f'{{"batch_reference": "{req.bank_batch_reference}", "cleared_count": {len(cleared_entries)}}}',
            f"Cycle marked disbursed with batch reference {req.bank_batch_reference}",
        )

    detail = await get_cycle_detail(conn, cid)
    return CycleActionStatusResponse(
        status="success",
        message=(
            f"Cycle disbursed for {detail.cycle.cleared_count} cleared employees "
            f"({detail.cycle.on_hold_count} kept on hold)."
        ),
        cycle=detail.cycle,
    )


# ---------------------------------------------------------------------------
# Employee Self-Service (ESS): Finalized Payslips & Live Provisional Preview
# ---------------------------------------------------------------------------
async def list_my_payslips(conn, employee_id: str) -> list[EmployeePayslipListItem]:
    emp_uuid = validate_uuid(employee_id, "employee_id")
    rows = await conn.fetch(
        """
        SELECT pe.id AS entry_id,
               pe.cycle_id,
               pc.cycle_name,
               pc.cycle_month,
               pc.cycle_year,
               pc.cycle_type,
               pc.status AS cycle_status,
               pe.payout_status,
               pe.days_worked,
               pe.ncp_days,
               pe.gross_earnings,
               pe.epf_employee_share,
               pe.esi_employee_share,
               pe.professional_tax,
               pe.tds_amount,
               pe.advance_recovery_amount,
               pe.reimbursements_amount,
               pe.net_payable,
               pe.deductions_breakdown,
               pe.paid_at,
               pe.bank_utr_number,
               pe.payslip_pdf_url
        FROM payroll_entries pe
        JOIN payroll_cycles pc ON pc.id = pe.cycle_id
        WHERE pe.employee_id = $1::uuid
          AND pe.status = 'CLEARED'
          AND pc.status IN ('APPROVED', 'DISBURSED')
        ORDER BY pc.cycle_year DESC, pc.cycle_month DESC, pc.created_at DESC;
        """,
        emp_uuid,
    )

    items: list[EmployeePayslipListItem] = []
    for r in rows:
        d_json = _parse_jsonb(r["deductions_breakdown"])
        total_ded = d_json.get("total_deductions")
        if total_ded is None:
            total_ded = float(
                Decimal(str(r["epf_employee_share"]))
                + Decimal(str(r["esi_employee_share"]))
                + Decimal(str(r["professional_tax"]))
                + Decimal(str(r["tds_amount"]))
                + Decimal(str(r["advance_recovery_amount"]))
            )
        items.append(
            EmployeePayslipListItem(
                entry_id=str(r["entry_id"]),
                cycle_id=str(r["cycle_id"]),
                cycle_name=r["cycle_name"],
                cycle_month=int(r["cycle_month"]),
                cycle_year=int(r["cycle_year"]),
                cycle_type=r["cycle_type"],
                cycle_status=r["cycle_status"],
                payout_status=r["payout_status"],
                days_worked=float(r["days_worked"]),
                ncp_days=float(r["ncp_days"]),
                gross_earnings=float(r["gross_earnings"]),
                total_deductions=float(total_ded),
                reimbursements_amount=float(r["reimbursements_amount"]),
                net_payable=float(r["net_payable"]),
                paid_at=_iso(r["paid_at"]),
                bank_utr_number=r["bank_utr_number"],
                payslip_pdf_url=r["payslip_pdf_url"],
            )
        )
    return items


async def get_my_payslip_detail(
    conn, employee_id: str, entry_id: str
) -> EmployeePayslipDetailResponse:
    emp_uuid = validate_uuid(employee_id, "employee_id")
    eid = validate_uuid(entry_id, "entry_id")

    entry_row = await conn.fetchrow(
        """
        SELECT pe.*,
               u.full_name AS employee_name,
               u.email AS employee_email,
               d.name AS department,
               u.designation,
               u.pan_number,
               u.uan_number
        FROM payroll_entries pe
        JOIN payroll_cycles pc ON pc.id = pe.cycle_id
        JOIN users u ON u.id = pe.employee_id
        LEFT JOIN departments d ON d.id = u.department_id
        WHERE pe.id = $1::uuid
          AND pe.employee_id = $2::uuid
          AND pe.status = 'CLEARED'
          AND pc.status IN ('APPROVED', 'DISBURSED');
        """,
        eid,
        emp_uuid,
    )
    if not entry_row:
        raise HTTPException(
            status_code=404, detail="Finalized payslip not found for your account."
        )

    cycle_row = await conn.fetchrow(
        "SELECT * FROM payroll_cycles WHERE id = $1::uuid;",
        entry_row["cycle_id"],
    )

    try:
        company = await get_company_statutory_details(conn)
    except HTTPException:
        company = None

    try:
        bank = await get_employee_bank_details(conn, emp_uuid)
    except HTTPException:
        bank = None

    return EmployeePayslipDetailResponse(
        entry=_row_to_entry(entry_row),
        cycle=_row_to_cycle_summary(cycle_row),
        company=company,
        bank_details=bank,
    )


def _to_json_numbers(val: Any) -> Any:
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    if isinstance(val, dict):
        return {k: _to_json_numbers(v) for k, v in val.items()}
    if isinstance(val, (list, tuple, set)):
        return [_to_json_numbers(x) for x in val]
    return val


async def get_my_provisional_preview(
    conn,
    employee_id: str,
    *,
    month: Optional[int] = None,
    year: Optional[int] = None,
    as_of_date: Optional[date] = None,
) -> ProvisionalPreviewResponse:
    """
    Real-time month-to-date earnings projection using Phase 3 read-only adapters
    and Phase 2 deterministic calculation engine. Zero writes to PostgreSQL.
    """
    emp_uuid = validate_uuid(employee_id, "employee_id")
    today_ist = datetime.now(IST).date()
    ref_as_of = as_of_date or today_ist
    target_month = month or ref_as_of.month
    target_year = year or ref_as_of.year

    last_day = calendar.monthrange(target_year, target_month)[1]
    start_dt = date(target_year, target_month, 1)
    end_dt = date(target_year, target_month, last_day)
    effective_as_of = min(ref_as_of, end_dt)

    ingestion = await fetch_cycle_employee_inputs(
        conn,
        cycle_month=target_month,
        cycle_year=target_year,
        start_date=start_dt,
        end_date=end_dt,
        cutoff_date=end_dt,
        employee_ids=[emp_uuid],
        as_of_date=effective_as_of,
        strict_attendance=False,
    )

    if not ingestion.bundles:
        return ProvisionalPreviewResponse(
            has_salary_structure=False,
            message="No active Indian salary structure is assigned to your profile yet.",
            cycle_month=target_month,
            cycle_year=target_year,
            as_of_date=effective_as_of.isoformat(),
        )

    bundle = ingestion.bundles[0]
    calc = calculate_monthly_payroll(bundle.engine_input)

    return ProvisionalPreviewResponse(
        has_salary_structure=True,
        cycle_month=target_month,
        cycle_year=target_year,
        as_of_date=effective_as_of.isoformat(),
        attendance_summary=_to_json_numbers(bundle.attendance_summary.model_dump()),
        leave_summary=_to_json_numbers(bundle.leave_summary.model_dump()),
        calculation=_to_json_numbers(calc.model_dump()),
        warnings=list(bundle.missing_statutory_warnings),
    )
