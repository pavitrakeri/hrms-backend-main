"""
Corporate Bank NEFT / RTGS Bulk Payout CSV Exporter (Phase 5).

Generates bulk upload CSV files compatible with Indian corporate banking portals
(HDFC, ICICI, SBI) containing validated 11-char IFSC codes, account numbers,
beneficiary names, and RBI NEFT/RTGS mode routing (`RTGS` >= Rs. 2,00,000).

Compliance guarantees:
  - Allowed only for APPROVED or DISBURSED payroll cycles.
  - Strictly excludes `ON_HOLD` records (`WHERE pe.status = 'CLEARED'`).
  - Validates presence of account number + 11-char Indian IFSC for BANK_TRANSFER rows.
  - Guards CSV text cells against spreadsheet formula injection.
"""

from __future__ import annotations

import csv
import io
import re
from decimal import Decimal
from typing import Any, Mapping, Sequence

from fastapi import HTTPException
from pydantic import BaseModel

from app.datamodels.payroll_v2_models import IFSC_REGEX
from app.features.payroll_v2.engine_india import round_paisa
from app.features.payroll_v2.master_service import validate_uuid

EXPORTABLE_CYCLE_STATUSES = frozenset({"APPROVED", "DISBURSED"})
RTGS_MINIMUM_AMOUNT = Decimal("200000.00")


class BankExportBundle(BaseModel):
    filename: str
    content: str
    beneficiary_count: int
    total_payout_amount: Decimal


def _col(r: Any, key: str, default: Any = None) -> Any:
    try:
        return r[key]
    except (KeyError, IndexError):
        return default


def _csv_safe(val: str) -> str:
    """Prevent spreadsheet formula injection while preserving clean text."""
    text = " ".join((val or "").split())
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return f"'{text}"
    return text


def build_bank_neft_csv(
    rows: Sequence[Mapping[str, Any]],
    *,
    cycle_month: int,
    cycle_year: int,
    cycle_type: str,
    cycle_name: str,
) -> BankExportBundle:
    """
    Pure deterministic formatter turning CLEARED rows + bank details into bulk payout CSV.
    """
    invalid_bank_employees: list[str] = []
    valid_rows: list[list[str]] = []
    total_amount = Decimal("0.00")

    sr_no = 0
    for r in rows:
        net = round_paisa(Decimal(str(r["net_payable"])))
        if net <= Decimal("0.00"):
            continue

        emp_name = str(_col(r, "account_holder_name") or _col(r, "full_name") or "Employee")
        emp_full_name = str(_col(r, "full_name") or emp_name)
        payment_method = str(_col(r, "payment_method") or "").strip().upper()
        acct_no = str(_col(r, "account_number") or "").strip()
        ifsc = str(_col(r, "ifsc_code") or "").strip().upper()
        bank_name = str(_col(r, "bank_name") or "").strip()
        email = str(_col(r, "email") or "").strip()
        entry_id = str(r["id"])

        if not payment_method:
            invalid_bank_employees.append(f"{emp_full_name} (missing bank details)")
            continue

        if payment_method == "BANK_TRANSFER":
            if not acct_no or not IFSC_REGEX.match(ifsc):
                invalid_bank_employees.append(
                    f"{emp_full_name} (missing account number or invalid IFSC '{ifsc}')"
                )
                continue
            mode = "RTGS" if net >= RTGS_MINIMUM_AMOUNT else "NEFT"
        else:
            mode = payment_method

        sr_no += 1
        total_amount = round_paisa(total_amount + net)
        tx_ref = f"AIM-{cycle_year}{cycle_month:02d}-{entry_id[:8].upper()}"
        narration = f"Salary {cycle_name}"[:50]

        valid_rows.append(
            [
                str(sr_no),
                mode,
                _csv_safe(emp_name),
                _csv_safe(acct_no),
                _csv_safe(ifsc),
                _csv_safe(bank_name),
                f"{net:.2f}",
                tx_ref,
                _csv_safe(narration),
                _csv_safe(email),
            ]
        )

    if invalid_bank_employees:
        raise HTTPException(
            status_code=422,
            detail=(
                "Cannot generate Bank NEFT/RTGS export due to incomplete banking KYC: "
                + "; ".join(invalid_bank_employees)
            ),
        )

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(
        [
            "Sr No",
            "Payment Mode",
            "Beneficiary Name",
            "Beneficiary Account Number",
            "IFSC Code",
            "Bank Name",
            "Amount (INR)",
            "Transaction Reference",
            "Narration",
            "Employee Email",
        ]
    )
    writer.writerows(valid_rows)

    safe_type = re.sub(r"[^A-Za-z0-9]", "_", cycle_type or "REGULAR").upper()
    filename = f"Aimploy_Bank_Payout_{cycle_year}_{cycle_month:02d}_{safe_type}.csv"
    return BankExportBundle(
        filename=filename,
        content=buf.getvalue(),
        beneficiary_count=sr_no,
        total_payout_amount=total_amount,
    )


async def generate_cycle_bank_export(conn, cycle_id: str) -> BankExportBundle:
    """
    Fetch APPROVED/DISBURSED cycle + CLEARED entries joined with primary bank accounts.
    """
    cid = validate_uuid(cycle_id, "cycle_id")
    cycle = await conn.fetchrow(
        """
        SELECT id, cycle_name, cycle_month, cycle_year, cycle_type, status
        FROM payroll_cycles
        WHERE id = $1::uuid;
        """,
        cid,
    )
    if not cycle:
        raise HTTPException(status_code=404, detail="Payroll cycle not found.")
    if cycle["status"] not in EXPORTABLE_CYCLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Bank payout export is available only for APPROVED or DISBURSED cycles "
                f"(current status: '{cycle['status']}')."
            ),
        )

    rows = await conn.fetch(
        """
        SELECT pe.id,
               pe.employee_id,
               pe.net_payable,
               u.full_name,
               u.email,
               ebd.payment_method,
               ebd.account_holder_name,
               ebd.bank_name,
               ebd.account_number,
               ebd.ifsc_code
        FROM payroll_entries pe
        JOIN users u ON u.id = pe.employee_id
        LEFT JOIN employee_bank_details ebd
               ON ebd.employee_id = pe.employee_id
              AND ebd.is_primary = true
        WHERE pe.cycle_id = $1::uuid
          AND pe.status = 'CLEARED'
        ORDER BY u.full_name ASC;
        """,
        cid,
    )
    if not rows:
        raise HTTPException(
            status_code=400, detail="No CLEARED entries found for bank export."
        )

    return build_bank_neft_csv(
        rows,
        cycle_month=int(cycle["cycle_month"]),
        cycle_year=int(cycle["cycle_year"]),
        cycle_type=str(cycle["cycle_type"]),
        cycle_name=str(cycle["cycle_name"]),
    )
