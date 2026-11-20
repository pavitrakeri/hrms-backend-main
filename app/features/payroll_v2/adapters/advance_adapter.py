"""
Salary Advance & EMI Recovery Adapter (Read-Only).

Reads active salary advance schedules from `employee_salary_advances` and caps
each scheduled recovery at `min(monthly_emi_amount, remaining_balance)` so the
final installment never over-deducts past the loan balance.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Mapping

from pydantic import BaseModel, Field

ZERO_PAISA = Decimal("0.00")
ONE_PAISA = Decimal("0.01")


def _round_paisa(val: Decimal) -> Decimal:
    return val.quantize(ONE_PAISA, rounding=ROUND_HALF_UP)


class AdvanceLoanRow(BaseModel):
    advance_id: str
    principal_amount: Decimal
    monthly_emi_amount: Decimal
    total_installments: int
    paid_installments: int
    remaining_balance: Decimal


class ScheduledAdvanceRecovery(BaseModel):
    advance_id: str
    scheduled_amount: Decimal
    remaining_balance_before: Decimal


class EmployeeAdvanceSummary(BaseModel):
    employee_id: str
    total_scheduled_emi: Decimal = ZERO_PAISA
    advance_recoveries: list[ScheduledAdvanceRecovery] = Field(default_factory=list)


def compute_advance_summary(
    *,
    employee_id: str,
    loans: Iterable[AdvanceLoanRow],
) -> EmployeeAdvanceSummary:
    recoveries: list[ScheduledAdvanceRecovery] = []
    total = ZERO_PAISA

    for loan in loans:
        if loan.remaining_balance <= ZERO_PAISA or loan.paid_installments >= loan.total_installments:
            continue
        due = _round_paisa(min(loan.monthly_emi_amount, loan.remaining_balance))
        if due <= ZERO_PAISA:
            continue
        recoveries.append(
            ScheduledAdvanceRecovery(
                advance_id=loan.advance_id,
                scheduled_amount=due,
                remaining_balance_before=loan.remaining_balance,
            )
        )
        total = _round_paisa(total + due)

    return EmployeeAdvanceSummary(
        employee_id=employee_id,
        total_scheduled_emi=total,
        advance_recoveries=recoveries,
    )


async def fetch_employee_advances(
    conn,
    *,
    employee_id: str,
) -> EmployeeAdvanceSummary:
    """Fetch active EMI recovery schedules for one employee (read-only)."""
    rows = await conn.fetch(
        """
        SELECT id::text AS advance_id,
               principal_amount,
               monthly_emi_amount,
               total_installments,
               paid_installments,
               remaining_balance
        FROM employee_salary_advances
        WHERE employee_id = $1::uuid
          AND status = 'ACTIVE'
          AND remaining_balance > 0
          AND paid_installments < total_installments
        ORDER BY created_at ASC;
        """,
        employee_id,
    )
    loans = [
        AdvanceLoanRow(
            advance_id=r["advance_id"],
            principal_amount=Decimal(str(r["principal_amount"])),
            monthly_emi_amount=Decimal(str(r["monthly_emi_amount"])),
            total_installments=int(r["total_installments"]),
            paid_installments=int(r["paid_installments"]),
            remaining_balance=Decimal(str(r["remaining_balance"])),
        )
        for r in rows
    ]
    return compute_advance_summary(employee_id=employee_id, loans=loans)


async def fetch_advances_batch(
    conn,
    *,
    employee_ids: list[str],
) -> Mapping[str, EmployeeAdvanceSummary]:
    """Batch-fetch active advance EMI schedules across multiple employees (read-only)."""
    if not employee_ids:
        return {}

    rows = await conn.fetch(
        """
        SELECT employee_id::text AS employee_id,
               id::text AS advance_id,
               principal_amount,
               monthly_emi_amount,
               total_installments,
               paid_installments,
               remaining_balance
        FROM employee_salary_advances
        WHERE employee_id = ANY($1::uuid[])
          AND status = 'ACTIVE'
          AND remaining_balance > 0
          AND paid_installments < total_installments
        ORDER BY employee_id, created_at ASC;
        """,
        employee_ids,
    )

    grouped: dict[str, list[AdvanceLoanRow]] = {eid: [] for eid in employee_ids}
    for r in rows:
        eid = r["employee_id"]
        if eid in grouped:
            grouped[eid].append(
                AdvanceLoanRow(
                    advance_id=r["advance_id"],
                    principal_amount=Decimal(str(r["principal_amount"])),
                    monthly_emi_amount=Decimal(str(r["monthly_emi_amount"])),
                    total_installments=int(r["total_installments"]),
                    paid_installments=int(r["paid_installments"]),
                    remaining_balance=Decimal(str(r["remaining_balance"])),
                )
            )

    return {
        eid: compute_advance_summary(employee_id=eid, loans=grouped[eid])
        for eid in employee_ids
    }
