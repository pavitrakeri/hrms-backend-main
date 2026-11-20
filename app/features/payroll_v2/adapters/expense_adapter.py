"""
Expense / Reimbursement Ingestion Adapter (Read-Only).

Strictly selects claims from `reimbursements` that are:
  1. Fully approved (`status = 'approved'`)
  2. Not yet disbursed (`paid_at IS NULL`)
  3. Submitted/incurred on or before the cycle `cutoff_date` (`expense_date <= cutoff_date`)

Prevents both double-payout of previously paid claims and accidental inclusion
of unapproved/rejected/queried claims.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Mapping, Optional

from pydantic import BaseModel, Field

ZERO_PAISA = Decimal("0.00")
ONE_PAISA = Decimal("0.01")


def _round_paisa(val: Decimal) -> Decimal:
    return val.quantize(ONE_PAISA, rounding=ROUND_HALF_UP)


class ApprovedReimbursementItem(BaseModel):
    reimbursement_id: str
    category: str
    subcategory: Optional[str] = None
    amount: Decimal
    expense_date: date
    decided_at: Optional[datetime] = None


class EmployeeExpenseSummary(BaseModel):
    employee_id: str
    total_approved_reimbursements: Decimal = ZERO_PAISA
    reimbursement_ids: list[str] = Field(default_factory=list)
    items: list[ApprovedReimbursementItem] = Field(default_factory=list)


def aggregate_employee_expenses(
    *,
    employee_id: str,
    items: Iterable[ApprovedReimbursementItem],
) -> EmployeeExpenseSummary:
    item_list = list(items)
    total = _round_paisa(sum((i.amount for i in item_list), ZERO_PAISA))
    ids = [i.reimbursement_id for i in item_list]
    return EmployeeExpenseSummary(
        employee_id=employee_id,
        total_approved_reimbursements=total,
        reimbursement_ids=ids,
        items=item_list,
    )


async def fetch_employee_pending_reimbursements(
    conn,
    *,
    employee_id: str,
    cutoff_date: date,
) -> EmployeeExpenseSummary:
    """Fetch approved, unpaid reimbursements up to `cutoff_date` for one employee."""
    rows = await conn.fetch(
        """
        SELECT id::text AS reimbursement_id,
               category,
               subcategory,
               amount,
               expense_date,
               decided_at
        FROM reimbursements
        WHERE user_id = $1::uuid
          AND status = 'approved'
          AND paid_at IS NULL
          AND expense_date <= $2
        ORDER BY expense_date ASC, created_at ASC;
        """,
        employee_id,
        cutoff_date,
    )
    items = [
        ApprovedReimbursementItem(
            reimbursement_id=r["reimbursement_id"],
            category=r["category"],
            subcategory=r["subcategory"],
            amount=Decimal(str(r["amount"])),
            expense_date=r["expense_date"],
            decided_at=r["decided_at"],
        )
        for r in rows
    ]
    return aggregate_employee_expenses(employee_id=employee_id, items=items)


async def fetch_reimbursements_batch(
    conn,
    *,
    employee_ids: list[str],
    cutoff_date: date,
) -> Mapping[str, EmployeeExpenseSummary]:
    """Batch-fetch approved, unpaid reimbursements up to `cutoff_date`."""
    if not employee_ids:
        return {}

    rows = await conn.fetch(
        """
        SELECT user_id::text AS employee_id,
               id::text AS reimbursement_id,
               category,
               subcategory,
               amount,
               expense_date,
               decided_at
        FROM reimbursements
        WHERE user_id = ANY($1::uuid[])
          AND status = 'approved'
          AND paid_at IS NULL
          AND expense_date <= $2
        ORDER BY user_id, expense_date ASC, created_at ASC;
        """,
        employee_ids,
        cutoff_date,
    )

    grouped: dict[str, list[ApprovedReimbursementItem]] = {eid: [] for eid in employee_ids}
    for r in rows:
        eid = r["employee_id"]
        if eid in grouped:
            grouped[eid].append(
                ApprovedReimbursementItem(
                    reimbursement_id=r["reimbursement_id"],
                    category=r["category"],
                    subcategory=r["subcategory"],
                    amount=Decimal(str(r["amount"])),
                    expense_date=r["expense_date"],
                    decided_at=r["decided_at"],
                )
            )

    return {
        eid: aggregate_employee_expenses(employee_id=eid, items=grouped[eid])
        for eid in employee_ids
    }
