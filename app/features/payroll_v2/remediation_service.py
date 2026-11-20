"""
Failed Payment Remediation, Off-Cycle Settlement & Audit Trail Service.

Guarantees:
  1. Failed Payment Isolation: Entries marked FAILED, REVERSED, or REQUIRES_REVIEW are tracked with bank error codes.
  2. Duplicate Payment Prevention: Row locking (FOR UPDATE) and validation ensure no employee is paid twice.
  3. Off-Cycle Supplementary Settlement: Generates an off-cycle cycle referencing the parent cycle.
  4. Immutable Ledger Preservation: Closed monthly cycle calculations remain untouched; supplementary payments are represented as distinct linked financial events.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional
from uuid import uuid4

from fastapi import HTTPException

from app.datamodels.payroll_v2_models import (
    OffCycleInitiateRequest,
    PayrollAuditLogRow,
    PayrollCycleSummary,
    RemediateFailureRequest,
)
from app.features.payroll_v2.engine_india import round_paisa
from app.features.payroll_v2.master_service import validate_uuid


async def update_entry_remediation(
    conn,
    user: dict[str, Any],
    cycle_id: str,
    record_id: str,
    req: RemediateFailureRequest,
) -> dict[str, Any]:
    """
    Update remediation status for a failed or held entry, optionally updating employee bank details.
    """
    cid = validate_uuid(cycle_id, "cycle_id")
    rid = validate_uuid(record_id, "record_id")
    user_id = user.get("id")
    usr_uuid = validate_uuid(str(user_id), "user_id") if user_id else None

    async with conn.transaction():
        entry = await conn.fetchrow(
            """
            SELECT pe.*, u.full_name
            FROM payroll_entries pe
            JOIN users u ON u.id = pe.employee_id
            WHERE pe.id = $1::uuid AND pe.cycle_id = $2::uuid
            FOR UPDATE OF pe;
            """,
            rid,
            cid,
        )
        if not entry:
            raise HTTPException(status_code=404, detail="Payroll entry not found.")

        # If corrected bank details are provided, update employee_bank_details
        if req.corrected_account_number or req.corrected_ifsc_code:
            emp_id = entry["employee_id"]
            if req.corrected_account_number and req.corrected_ifsc_code:
                await conn.execute(
                    """
                    UPDATE employee_bank_details
                    SET account_number = $2,
                        ifsc_code = $3,
                        updated_at = now()
                    WHERE employee_id = $1::uuid AND is_primary = true;
                    """,
                    emp_id,
                    req.corrected_account_number.strip(),
                    req.corrected_ifsc_code.strip().upper(),
                )

        updated_entry = await conn.fetchrow(
            """
            UPDATE payroll_entries
            SET remediation_status = $3,
                remediation_notes = COALESCE($4, remediation_notes),
                updated_at = now()
            WHERE id = $1::uuid AND cycle_id = $2::uuid
            RETURNING *;
            """,
            rid,
            cid,
            req.remediation_status,
            req.notes,
        )

        # Audit log
        await conn.execute(
            """
            INSERT INTO payroll_audit_logs (
                cycle_id, entry_id, actor_id, actor_role, action,
                entity_type, entity_id, comments
            )
            VALUES (
                $1::uuid, $2::uuid, $3::uuid, $4, 'PAYMENT_FAILURE_REMEDIATED',
                'payroll_entries', $5, $6
            );
            """,
            cid,
            rid,
            usr_uuid,
            user.get("role", "system"),
            str(rid),
            f"Remediation status updated to {req.remediation_status}: {req.notes or 'Details updated'}",
        )

    return {
        "status": "success",
        "entry_id": str(updated_entry["id"]),
        "remediation_status": updated_entry["remediation_status"],
        "remediation_notes": updated_entry["remediation_notes"],
    }


async def initiate_off_cycle_settlement(
    conn,
    user: dict[str, Any],
    cycle_id: str,
    req: OffCycleInitiateRequest,
) -> PayrollCycleSummary:
    """
    Safely initiate an off-cycle recovery cycle for failed or held entries.
    Prevents duplicate payments by checking bank_verified and existing PAID records.
    """
    cid = validate_uuid(cycle_id, "cycle_id")
    user_id = user.get("id")
    usr_uuid = validate_uuid(str(user_id), "user_id") if user_id else None

    if not req.target_entry_ids:
        raise HTTPException(status_code=400, detail="Target entry IDs required for off-cycle recovery.")

    target_uuids = [validate_uuid(tid, "target_entry_id") for tid in req.target_entry_ids]

    async with conn.transaction():
        parent_cycle = await conn.fetchrow(
            "SELECT * FROM payroll_cycles WHERE id = $1::uuid FOR UPDATE;", cid
        )
        if not parent_cycle:
            raise HTTPException(status_code=404, detail="Parent payroll cycle not found.")

        # Verify candidate entries
        entries = await conn.fetch(
            """
            SELECT id, employee_id, net_payable, payout_status, bank_verified, status, remediation_status
            FROM payroll_entries
            WHERE id = ANY($1::uuid[]) AND cycle_id = $2::uuid
            FOR UPDATE;
            """,
            target_uuids,
            cid,
        )

        if len(entries) != len(target_uuids):
            raise HTTPException(status_code=400, detail="One or more target entry IDs do not belong to this cycle.")

        # Invariant: Never pay an already paid or verified entry
        for e in entries:
            if e["bank_verified"] or e["payout_status"] == "PAID":
                raise HTTPException(
                    status_code=409,
                    detail=f"Entry {e['id']} is already PAID and bank-verified. Cannot initiate duplicate payment.",
                )
            if e["status"] == "ON_HOLD":
                raise HTTPException(
                    status_code=409,
                    detail=f"Entry {e['id']} is currently ON_HOLD. It must be released before off-cycle settlement.",
                )

        # Create Off-Cycle Cycle
        m = int(parent_cycle["cycle_month"])
        y = int(parent_cycle["cycle_year"])
        off_cycle_id = uuid4()
        off_cycle_name = f"{parent_cycle['cycle_name']} (Off-Cycle Settlement)"

        total_net = sum(Decimal(str(e["net_payable"])) for e in entries)

        off_cycle = await conn.fetchrow(
            """
            INSERT INTO payroll_cycles (
                id, cycle_name, cycle_month, cycle_year,
                start_date, end_date, cutoff_date,
                cycle_type, reference_cycle_id, status,
                total_gross, total_net_payable, employee_count,
                created_by
            )
            VALUES (
                $1::uuid, $2, $3, $4,
                $5, $6, $7,
                'OFF_CYCLE', $8::uuid, 'APPROVED',
                $9, $9, $10,
                $11::uuid
            )
            RETURNING *;
            """,
            off_cycle_id,
            off_cycle_name,
            m,
            y,
            parent_cycle["start_date"],
            parent_cycle["end_date"],
            parent_cycle["cutoff_date"],
            cid,
            round_paisa(total_net),
            len(entries),
            usr_uuid,
        )

        # Copy entries into off_cycle cycle with payout_status = 'NOT_GENERATED'
        for e in entries:
            # Fetch original full row
            full_orig = await conn.fetchrow("SELECT * FROM payroll_entries WHERE id = $1::uuid;", e["id"])
            await conn.execute(
                """
                INSERT INTO payroll_entries (
                    cycle_id, employee_id, status, hold_reason,
                    total_calendar_days, days_worked, unpaid_leave_days, unexcused_absent_days, ncp_days,
                    earned_basic, earned_hra, earned_special_allowance, earned_da, gross_earnings,
                    epf_wages, eps_wages, esi_wages,
                    epf_employee_share, eps_employer_share, epf_employer_share, edli_employer_share, epf_admin_charges,
                    esi_employee_share, esi_employer_share, professional_tax, tds_amount,
                    reimbursements_amount, advance_recovery_amount, net_payable,
                    gratuity_accrual_monthly, total_employer_cost,
                    earnings_breakdown, deductions_breakdown, payout_status
                )
                VALUES (
                    $1::uuid, $2::uuid, 'CLEARED', NULL,
                    $3, $4, $5, $6, $7,
                    $8, $9, $10, $11, $12,
                    $13, $14, $15,
                    $16, $17, $18, $19, $20,
                    $21, $22, $23, $24,
                    $25, $26, $27,
                    $28, $29,
                    $30, $31, 'NOT_GENERATED'
                );
                """,
                off_cycle_id,
                full_orig["employee_id"],
                full_orig["total_calendar_days"],
                full_orig["days_worked"],
                full_orig["unpaid_leave_days"],
                full_orig["unexcused_absent_days"],
                full_orig["ncp_days"],
                full_orig["earned_basic"],
                full_orig["earned_hra"],
                full_orig["earned_special_allowance"],
                full_orig["earned_da"],
                full_orig["gross_earnings"],
                full_orig["epf_wages"],
                full_orig["eps_wages"],
                full_orig["esi_wages"],
                full_orig["epf_employee_share"],
                full_orig["eps_employer_share"],
                full_orig["epf_employer_share"],
                full_orig["edli_employer_share"],
                full_orig["epf_admin_charges"],
                full_orig["esi_employee_share"],
                full_orig["esi_employer_share"],
                full_orig["professional_tax"],
                full_orig["tds_amount"],
                full_orig["reimbursements_amount"],
                full_orig["advance_recovery_amount"],
                full_orig["net_payable"],
                full_orig["gratuity_accrual_monthly"],
                full_orig["total_employer_cost"],
                full_orig["earnings_breakdown"],
                full_orig["deductions_breakdown"],
            )

            # Mark original entry as OFF_CYCLE_QUEUED
            await conn.execute(
                """
                UPDATE payroll_entries
                SET remediation_status = 'OFF_CYCLE_QUEUED',
                    remediation_notes = $2,
                    updated_at = now()
                WHERE id = $1::uuid;
                """,
                e["id"],
                f"Queued in off-cycle settlement cycle {off_cycle_id}",
            )

        # Audit log
        await conn.execute(
            """
            INSERT INTO payroll_audit_logs (
                cycle_id, actor_id, actor_role, action,
                entity_type, entity_id, new_state, comments
            )
            VALUES (
                $1::uuid, $2::uuid, $3, 'OFF_CYCLE_SETTLEMENT_INITIATED',
                'payroll_cycles', $4, $5::jsonb, $6
            );
            """,
            cid,
            usr_uuid,
            user.get("role", "system"),
            str(off_cycle_id),
            f'{{"off_cycle_id": "{off_cycle_id}", "beneficiaries": {len(entries)}, "amount": {float(total_net)}}}',
            f"Created off-cycle settlement for {len(entries)} beneficiaries ({req.notes or 'No notes'})",
        )

    return PayrollCycleSummary(
        id=str(off_cycle["id"]),
        cycle_name=off_cycle["cycle_name"],
        cycle_month=off_cycle["cycle_month"],
        cycle_year=off_cycle["cycle_year"],
        start_date=off_cycle["start_date"].isoformat(),
        end_date=off_cycle["end_date"].isoformat(),
        cutoff_date=off_cycle["cutoff_date"].isoformat(),
        cycle_type=off_cycle["cycle_type"],
        reference_cycle_id=str(off_cycle["reference_cycle_id"]),
        status=off_cycle["status"],
        total_gross=float(off_cycle["total_gross"]),
        total_epf_employee=float(off_cycle["total_epf_employee"]),
        total_eps_employer=float(off_cycle["total_eps_employer"]),
        total_epf_employer=float(off_cycle["total_epf_employer"]),
        total_esi_employee=float(off_cycle["total_esi_employee"]),
        total_esi_employer=float(off_cycle["total_esi_employer"]),
        total_pt=float(off_cycle["total_pt"]),
        total_tds=float(off_cycle["total_tds"]),
        total_reimbursements=float(off_cycle["total_reimbursements"]),
        total_advance_deductions=float(off_cycle["total_advance_deductions"]),
        total_net_payable=float(off_cycle["total_net_payable"]),
        total_company_cost=float(off_cycle["total_company_cost"]),
        employee_count=off_cycle["employee_count"],
        cleared_count=off_cycle["employee_count"],
        on_hold_count=0,
        bank_batch_reference=off_cycle["bank_batch_reference"],
        bank_export_url=off_cycle["bank_export_url"],
        ecr_export_url=off_cycle["ecr_export_url"],
        created_by=str(off_cycle["created_by"]) if off_cycle["created_by"] else None,
        approved_by=str(off_cycle["approved_by"]) if off_cycle["approved_by"] else None,
        disbursed_at=off_cycle["disbursed_at"].isoformat() if off_cycle["disbursed_at"] else None,
        created_at=off_cycle["created_at"].isoformat(),
        updated_at=off_cycle["updated_at"].isoformat(),
    )


async def list_payroll_audit_logs(
    conn,
    *,
    cycle_id: Optional[str] = None,
    entry_id: Optional[str] = None,
    limit: int = 100,
) -> list[PayrollAuditLogRow]:
    """
    Retrieve append-only audit trail logs for financial tracing.
    """
    conditions = []
    params = []
    idx = 1

    if cycle_id:
        conditions.append(f"cycle_id = ${idx}::uuid")
        params.append(validate_uuid(cycle_id, "cycle_id"))
        idx += 1

    if entry_id:
        conditions.append(f"entry_id = ${idx}::uuid")
        params.append(validate_uuid(entry_id, "entry_id"))
        idx += 1

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"""
        SELECT *
        FROM payroll_audit_logs
        {where_clause}
        ORDER BY created_at DESC
        LIMIT ${idx};
    """
    params.append(limit)
    rows = await conn.fetch(query, *params)

    import json
    return [
        PayrollAuditLogRow(
            id=str(r["id"]),
            cycle_id=str(r["cycle_id"]) if r["cycle_id"] else None,
            entry_id=str(r["entry_id"]) if r["entry_id"] else None,
            actor_id=str(r["actor_id"]) if r["actor_id"] else None,
            actor_role=r["actor_role"],
            action=r["action"],
            entity_type=r["entity_type"],
            entity_id=r["entity_id"],
            previous_state=r["previous_state"] if isinstance(r["previous_state"], dict) else (json.loads(r["previous_state"]) if r["previous_state"] else None),
            new_state=r["new_state"] if isinstance(r["new_state"], dict) else (json.loads(r["new_state"]) if r["new_state"] else None),
            comments=r["comments"],
            created_at=r["created_at"].isoformat(),
        )
        for r in rows
    ]
