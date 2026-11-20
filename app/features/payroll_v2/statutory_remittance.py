"""
Statutory Remittance & Compliance Tracking Service (EPFO, ESIC, PT, TDS).

Implements:
  1. Automated liability rollup from calculated payroll entries (EE share, ER share, admin/EDLI charges).
  2. Statutory deadlines:
     - EPFO (EPF + EPS + EDLI + Admin): 15th of following month.
     - ESIC: 15th of following month.
     - TDS (Income Tax Sec 192): 7th of following month (30th April for March).
     - PT (Professional Tax): 20th of following month.
  3. Five-way reconciliation:
     - Calculated liability vs Declared return liability vs Challan amount vs Paid amount vs Acknowledged amount.
     - Discrepancies automatically flag REQUIRES_REVIEW or PARTIALLY_PAID.
  4. Legal identifiers: TRRN (EPFO), CRN (ESIC), BSR/Challan Serial (TDS), PRN Token (24Q), GRN (PT).
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional

from fastapi import HTTPException

from app.datamodels.payroll_v2_models import (
    StatutoryCategory,
    StatutoryRemittanceRow,
    StatutoryRemittanceUpdateRequest,
)
from app.features.payroll_v2.engine_india import round_paisa
from app.features.payroll_v2.master_service import get_financial_year_label, validate_uuid

ZERO = Decimal("0.00")


def _calculate_due_date(category: str, month: int, year: int) -> date:
    """Determine statutory due date under Indian labor & tax statutes."""
    # Following month
    if month == 12:
        next_month = 1
        next_year = year + 1
    else:
        next_month = month + 1
        next_year = year

    if category in ("EPFO", "ESIC"):
        # 15th of the following month
        return date(next_year, next_month, 15)
    elif category == "TDS":
        # 7th of the following month (except March which is April 30)
        if month == 3:
            return date(year, 4, 30)
        return date(next_year, next_month, 7)
    elif category == "PT":
        # Standard: 20th of following month (e.g. Karnataka, Maharashtra)
        return date(next_year, next_month, 20)
    return date(next_year, next_month, 15)


async def init_statutory_remittances_for_cycle(
    conn,
    cycle_id: str,
) -> list[StatutoryRemittanceRow]:
    """
    Roll up statutory liabilities for EPFO, ESIC, PT, and TDS from payroll_entries
    and initialize or update the compliance tracking ledger.
    """
    cid = validate_uuid(cycle_id, "cycle_id")
    cycle = await conn.fetchrow(
        "SELECT id, cycle_month, cycle_year FROM payroll_cycles WHERE id = $1::uuid;", cid
    )
    if not cycle:
        raise HTTPException(status_code=404, detail="Payroll cycle not found.")

    m = int(cycle["cycle_month"])
    y = int(cycle["cycle_year"])
    fy_label, _, _ = get_financial_year_label(m, y)

    # Aggregate statutory liabilities strictly from CLEARED entries
    agg = await conn.fetchrow(
        """
        SELECT 
            COALESCE(SUM(epf_employee_share), 0) AS epf_ee,
            COALESCE(SUM(eps_employer_share), 0) AS eps_er,
            COALESCE(SUM(epf_employer_share), 0) AS epf_er,
            COALESCE(SUM(edli_employer_share), 0) AS edli_er,
            COALESCE(SUM(epf_admin_charges), 0) AS epf_admin,
            COALESCE(SUM(esi_employee_share), 0) AS esi_ee,
            COALESCE(SUM(esi_employer_share), 0) AS esi_er,
            COALESCE(SUM(professional_tax), 0) AS pt_total,
            COALESCE(SUM(tds_amount), 0) AS tds_total
        FROM payroll_entries
        WHERE cycle_id = $1::uuid AND status = 'CLEARED';
        """,
        cid,
    )

    # EPFO total = EE (12%) + ER EPF (3.67%) + ER EPS (8.33%) + EDLI (0.5%) + Admin (0.5%)
    epfo_total = round_paisa(
        Decimal(str(agg["epf_ee"]))
        + Decimal(str(agg["eps_er"]))
        + Decimal(str(agg["epf_er"]))
        + Decimal(str(agg["edli_er"]))
        + Decimal(str(agg["epf_admin"]))
    )

    # ESIC total = EE (0.75%) + ER (3.25%)
    esic_total = round_paisa(
        Decimal(str(agg["esi_ee"])) + Decimal(str(agg["esi_er"]))
    )

    pt_total = round_paisa(Decimal(str(agg["pt_total"])))
    tds_total = round_paisa(Decimal(str(agg["tds_total"])))

    categories = [
        ("EPFO", epfo_total),
        ("ESIC", esic_total),
        ("PT", pt_total),
        ("TDS", tds_total),
    ]

    for cat, calc_amount in categories:
        due = _calculate_due_date(cat, m, y)
        await conn.execute(
            """
            INSERT INTO payroll_statutory_remittances (
                cycle_id, category, financial_year, wage_month, wage_year,
                calculated_liability, declared_liability, challan_amount,
                status, due_date
            )
            VALUES (
                $1::uuid, $2, $3, $4, $5,
                $6::numeric, $6::numeric, $6::numeric,
                CASE WHEN $6::numeric = 0 THEN 'RECONCILED' ELSE 'GENERATED' END, $7
            )
            ON CONFLICT (cycle_id, category) DO UPDATE
            SET calculated_liability = EXCLUDED.calculated_liability,
                declared_liability = CASE 
                    WHEN payroll_statutory_remittances.declared_liability = 0 
                    THEN EXCLUDED.calculated_liability 
                    ELSE payroll_statutory_remittances.declared_liability 
                END,
                challan_amount = CASE 
                    WHEN payroll_statutory_remittances.challan_amount = 0 
                    THEN EXCLUDED.calculated_liability 
                    ELSE payroll_statutory_remittances.challan_amount 
                END,
                due_date = EXCLUDED.due_date,
                updated_at = now();
            """,
            cid,
            cat,
            fy_label,
            m,
            y,
            calc_amount,
            due,
        )

    return await list_statutory_remittances_for_cycle(conn, cycle_id)


async def list_statutory_remittances_for_cycle(
    conn,
    cycle_id: str,
) -> list[StatutoryRemittanceRow]:
    """
    List statutory remittance tracking items with computed variance.
    """
    cid = validate_uuid(cycle_id, "cycle_id")
    rows = await conn.fetch(
        """
        SELECT *
        FROM payroll_statutory_remittances
        WHERE cycle_id = $1::uuid
        ORDER BY 
            CASE category 
                WHEN 'EPFO' THEN 1 
                WHEN 'ESIC' THEN 2 
                WHEN 'PT' THEN 3 
                WHEN 'TDS' THEN 4 
                ELSE 5 
            END;
        """,
        cid,
    )
    result = []
    for r in rows:
        calc = Decimal(str(r["calculated_liability"]))
        decl = Decimal(str(r["declared_liability"]))
        challan = Decimal(str(r["challan_amount"]))
        paid = Decimal(str(r["paid_amount"]))
        target = decl if decl > ZERO else calc
        diff = round_paisa(target - paid)

        result.append(
            StatutoryRemittanceRow(
                id=str(r["id"]),
                cycle_id=str(r["cycle_id"]),
                category=r["category"],
                financial_year=r["financial_year"],
                wage_month=r["wage_month"],
                wage_year=r["wage_year"],
                calculated_liability=float(calc),
                declared_liability=float(decl),
                challan_amount=float(challan),
                paid_amount=float(paid),
                acknowledged_amount=float(r["acknowledged_amount"]),
                difference_amount=float(diff),
                status=r["status"],
                due_date=r["due_date"].isoformat(),
                payment_date=r["payment_date"].isoformat() if r["payment_date"] else None,
                filing_date=r["filing_date"].isoformat() if r["filing_date"] else None,
                external_reference=r["external_reference"],
                token_number=r["token_number"],
                notes=r["notes"],
                challan_document_id=str(r["challan_document_id"]) if r["challan_document_id"] else None,
                receipt_document_id=str(r["receipt_document_id"]) if r["receipt_document_id"] else None,
                reconciled_at=r["reconciled_at"].isoformat() if r["reconciled_at"] else None,
                updated_at=r["updated_at"].isoformat(),
            )
        )
    return result


async def update_statutory_remittance(
    conn,
    user: dict[str, Any],
    cycle_id: str,
    category: StatutoryCategory,
    req: StatutoryRemittanceUpdateRequest,
) -> StatutoryRemittanceRow:
    """
    Update statutory filing & payment status, validating reconciliation variances.
    """
    cid = validate_uuid(cycle_id, "cycle_id")
    user_id = user.get("id")
    usr_uuid = validate_uuid(str(user_id), "user_id") if user_id else None

    async with conn.transaction():
        rem = await conn.fetchrow(
            """
            SELECT * FROM payroll_statutory_remittances
            WHERE cycle_id = $1::uuid AND category = $2
            FOR UPDATE;
            """,
            cid,
            category,
        )
        if not rem:
            # Auto-initialize if not yet present
            await init_statutory_remittances_for_cycle(conn, cycle_id)
            rem = await conn.fetchrow(
                "SELECT * FROM payroll_statutory_remittances WHERE cycle_id = $1::uuid AND category = $2 FOR UPDATE;",
                cid,
                category,
            )

        calc = Decimal(str(rem["calculated_liability"]))
        decl = Decimal(str(req.declared_liability if req.declared_liability is not None else rem["declared_liability"]))
        challan = Decimal(str(req.challan_amount if req.challan_amount is not None else rem["challan_amount"]))
        paid = Decimal(str(req.paid_amount if req.paid_amount is not None else rem["paid_amount"]))
        ack = Decimal(str(req.acknowledged_amount if req.acknowledged_amount is not None else rem["acknowledged_amount"]))

        # Determine automated status if not explicitly overridden
        target = decl if decl > ZERO else calc
        auto_status = req.status or rem["status"]

        if req.status is None:
            if paid >= target and target > ZERO and (req.external_reference or rem["external_reference"]):
                if ack >= paid:
                    auto_status = "RECONCILED"
                else:
                    auto_status = "PAID"
            elif paid > ZERO and paid < target:
                auto_status = "PARTIALLY_PAID"
            elif challan > ZERO and paid == ZERO:
                auto_status = "PAYMENT_PENDING"
            elif abs(decl - calc) > Decimal("1.00"):
                auto_status = "REQUIRES_REVIEW"

        pay_date = date.fromisoformat(req.payment_date) if req.payment_date else rem["payment_date"]
        file_date = date.fromisoformat(req.filing_date) if req.filing_date else rem["filing_date"]

        updated = await conn.fetchrow(
            """
            UPDATE payroll_statutory_remittances
            SET declared_liability = $3,
                challan_amount = $4,
                paid_amount = $5,
                acknowledged_amount = $6,
                status = $7::text,
                payment_date = $8,
                filing_date = $9,
                external_reference = COALESCE($10::text, external_reference),
                token_number = COALESCE($11::text, token_number),
                notes = COALESCE($12::text, notes),
                reconciled_by = $13::uuid,
                reconciled_at = CASE WHEN $7::text IN ('PAID', 'RECONCILED') THEN now() ELSE reconciled_at END,
                updated_at = now()
            WHERE cycle_id = $1::uuid AND category = $2::text
            RETURNING *;
            """,
            cid,
            category,
            decl,
            challan,
            paid,
            ack,
            auto_status,
            pay_date,
            file_date,
            req.external_reference,
            req.token_number,
            req.notes,
            usr_uuid,
        )

        # Audit log
        await conn.execute(
            """
            INSERT INTO payroll_audit_logs (
                cycle_id, actor_id, actor_role, action,
                entity_type, entity_id, new_state, comments
            )
            VALUES (
                $1::uuid, $2::uuid, $3, 'STATUTORY_REMITTANCE_UPDATED',
                'payroll_statutory_remittances', $4, $5::jsonb, $6
            );
            """,
            cid,
            usr_uuid,
            user.get("role", "system"),
            str(updated["id"]),
            f'{{"category": "{category}", "status": "{auto_status}", "paid": {float(paid)}, "ref": "{req.external_reference or rem["external_reference"]}"}}',
            f"Updated {category} remittance to {auto_status} with paid amount ₹{paid}",
        )

    target_dec = decl if decl > ZERO else calc
    diff = round_paisa(target_dec - paid)

    return StatutoryRemittanceRow(
        id=str(updated["id"]),
        cycle_id=str(updated["cycle_id"]),
        category=updated["category"],
        financial_year=updated["financial_year"],
        wage_month=updated["wage_month"],
        wage_year=updated["wage_year"],
        calculated_liability=float(calc),
        declared_liability=float(decl),
        challan_amount=float(challan),
        paid_amount=float(paid),
        acknowledged_amount=float(ack),
        difference_amount=float(diff),
        status=updated["status"],
        due_date=updated["due_date"].isoformat(),
        payment_date=updated["payment_date"].isoformat() if updated["payment_date"] else None,
        filing_date=updated["filing_date"].isoformat() if updated["filing_date"] else None,
        external_reference=updated["external_reference"],
        token_number=updated["token_number"],
        notes=updated["notes"],
        challan_document_id=str(updated["challan_document_id"]) if updated["challan_document_id"] else None,
        receipt_document_id=str(updated["receipt_document_id"]) if updated["receipt_document_id"] else None,
        reconciled_at=updated["reconciled_at"].isoformat() if updated["reconciled_at"] else None,
        updated_at=updated["updated_at"].isoformat(),
    )
