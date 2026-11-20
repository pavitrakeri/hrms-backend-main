"""
Income Tax Form 24Q (Quarterly e-TDS Return) Exporter (Phase 5).

Generates NSDL / TRACES compliant caret (`^`) delimited text payloads covering:
  - `FH` : File Header (Deductor TAN, Corporate PAN, FY, AY, Quarter Q1-Q4)
  - `BH` : Batch Header (Payment Date, Total Deductees, Gross, Base TDS, Surcharge, 4% Cess)
  - `CD` : Challan Detail (Section 192B, Minor Head 200, Aggregate Tax Liability)
  - `DD` : Deductee Detail (Annexure I & II fields: PAN, Sec 192, Regime NEW/OLD,
           Sec 10 HRA exemption, Standard Deduction, Chapter VI-A deductions, Taxable Income)

Compliance guarantees:
  - Allowed only for APPROVED or DISBURSED payroll cycles.
  - Excludes all `ON_HOLD` entries (`WHERE pe.status = 'CLEARED'`).
  - Validates 10-char PAN for every CLEARED employee with `tds_amount > 0`.
"""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping, Sequence

from fastapi import HTTPException
from pydantic import BaseModel

from app.datamodels.payroll_v2_models import PAN_REGEX
from app.features.payroll_v2.adapters.employee_aggregator import get_financial_year_label
from app.features.payroll_v2.engine_india import ONE_PAISA, round_paisa
from app.features.payroll_v2.master_service import (
    get_company_statutory_details,
    validate_uuid,
)

EXPORTABLE_CYCLE_STATUSES = frozenset({"APPROVED", "DISBURSED"})
ZERO = Decimal("0.00")


class Form24QExportBundle(BaseModel):
    filename: str
    content: str
    financial_year: str
    assessment_year: str
    quarter: str
    deductee_count: int
    total_gross_paid: Decimal
    total_tds_deducted: Decimal


def _col(r: Any, key: str, default: Any = None) -> Any:
    try:
        return r[key]
    except (KeyError, IndexError):
        return default


def _parse_jsonb(val: Any) -> dict[str, Any]:
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _caret_safe(val: str) -> str:
    return " ".join((val or "").replace("^", " ").replace("\n", " ").split())


def get_tax_quarter_and_years(cycle_month: int, cycle_year: int) -> tuple[str, str, str]:
    """
    Map calendar month/year -> `(quarter, financial_year, assessment_year)`.
    Example: Nov 2026 -> `('Q3', '2026-2027', '2027-2028')`.
    """
    fy_label, fy_start, fy_end = get_financial_year_label(cycle_month, cycle_year)
    ay_label = f"{fy_start + 1}-{fy_end + 1}"

    if cycle_month in (4, 5, 6):
        quarter = "Q1"
    elif cycle_month in (7, 8, 9):
        quarter = "Q2"
    elif cycle_month in (10, 11, 12):
        quarter = "Q3"
    else:
        quarter = "Q4"

    return quarter, fy_label, ay_label


def _split_monthly_tax_components(
    monthly_tds: Decimal,
    tax_breakdown: Mapping[str, Any],
) -> tuple[Decimal, Decimal, Decimal]:
    """
    Decompose `monthly_tds` into `(base_tax, surcharge, health_edu_cess)`
    preserving exact sum: `base_tax + surcharge + health_edu_cess == monthly_tds`.
    """
    tds = round_paisa(monthly_tds)
    if tds <= ZERO:
        return ZERO, ZERO, ZERO

    annual_total = Decimal(str(tax_breakdown.get("annual_tax_liability") or "0"))
    if annual_total > ZERO:
        ann_base = Decimal(str(tax_breakdown.get("tax_after_rebate") or "0"))
        ann_sur = Decimal(str(tax_breakdown.get("surcharge") or "0"))
        sur = (tds * ann_sur / annual_total).quantize(ONE_PAISA, rounding=ROUND_HALF_UP)
        base_ratio = (tds * ann_base / annual_total).quantize(ONE_PAISA, rounding=ROUND_HALF_UP)
        cess = round_paisa(tds - base_ratio - sur)
        if cess < ZERO:
            cess = ZERO
            sur = min(sur, tds)
            base_ratio = round_paisa(tds - sur)
        return base_ratio, sur, cess

    # Standard 4% cess decomposition when no breakdown snapshot is stored
    base_tax = (tds / Decimal("1.04")).quantize(ONE_PAISA, rounding=ROUND_HALF_UP)
    cess = round_paisa(tds - base_tax)
    return base_tax, ZERO, cess


def build_form24q_content(
    rows: Sequence[Mapping[str, Any]],
    *,
    company_name: str,
    tan_number: str,
    corporate_pan: str,
    registered_address: str,
    cycle_name: str,
    cycle_month: int,
    cycle_year: int,
    payment_date: date,
) -> Form24QExportBundle:
    """
    Pure deterministic NSDL/TRACES Form 24Q `.txt` generator for cleared staff.
    """
    quarter, fy_label, ay_label = get_tax_quarter_and_years(cycle_month, cycle_year)
    missing_pan_names: list[str] = []

    dd_lines: list[str] = []
    tot_gross = ZERO
    tot_base = ZERO
    tot_sur = ZERO
    tot_cess = ZERO
    tot_tds = ZERO

    pay_dt_str = payment_date.strftime("%d-%m-%Y")
    sr_no = 0

    for r in rows:
        emp_name = str(_col(r, "full_name") or _col(r, "employee_name") or "Employee")
        raw_pan = str(_col(r, "pan_number") or "").strip().upper()
        monthly_tds = round_paisa(Decimal(str(r["tds_amount"])))
        monthly_gross = round_paisa(Decimal(str(r["gross_earnings"])))

        if monthly_tds > ZERO and not PAN_REGEX.match(raw_pan):
            missing_pan_names.append(emp_name)
            continue

        emp_pan = raw_pan if PAN_REGEX.match(raw_pan) else "PANNOTAVBL"
        d_json = _parse_jsonb(_col(r, "deductions_breakdown"))
        tb = d_json.get("tax_breakdown") if isinstance(d_json.get("tax_breakdown"), dict) else {}

        base_tax, sur, cess = _split_monthly_tax_components(monthly_tds, tb)
        regime = str(tb.get("regime") or d_json.get("tax_regime") or "NEW").upper()
        if regime not in ("NEW", "OLD"):
            regime = "NEW"

        proj_annual_gross = round_paisa(Decimal(str(tb.get("projected_annual_gross") or (monthly_gross * 12))))
        hra_exemption = round_paisa(Decimal(str(tb.get("hra_exemption") or "0")))
        std_ded = round_paisa(Decimal(str(tb.get("standard_deduction") or ("75000" if regime == "NEW" else "50000"))))
        chap_via = round_paisa(
            Decimal(str(tb.get("section_80c_allowed") or "0"))
            + Decimal(str(tb.get("section_80d_allowed") or "0"))
            + Decimal(str(tb.get("section_80ccd_1b_allowed") or "0"))
            + Decimal(str(tb.get("section_24b_allowed") or "0"))
        )
        taxable_inc = round_paisa(Decimal(str(tb.get("taxable_income") or "0")))
        annual_tax = round_paisa(Decimal(str(tb.get("annual_tax_liability") or (monthly_tds * 12))))

        sr_no += 1
        tot_gross = round_paisa(tot_gross + monthly_gross)
        tot_base = round_paisa(tot_base + base_tax)
        tot_sur = round_paisa(tot_sur + sur)
        tot_cess = round_paisa(tot_cess + cess)
        tot_tds = round_paisa(tot_tds + monthly_tds)

        dd_lines.append(
            "^".join(
                [
                    "DD",
                    str(sr_no),
                    "192",
                    emp_pan,
                    _caret_safe(emp_name.upper()),
                    pay_dt_str,
                    f"{monthly_gross:.2f}",
                    f"{base_tax:.2f}",
                    f"{sur:.2f}",
                    f"{cess:.2f}",
                    f"{monthly_tds:.2f}",
                    regime,
                    f"{proj_annual_gross:.2f}",
                    f"{hra_exemption:.2f}",
                    f"{std_ded:.2f}",
                    f"{chap_via:.2f}",
                    f"{taxable_inc:.2f}",
                    f"{annual_tax:.2f}",
                ]
            )
        )

    if missing_pan_names:
        raise HTTPException(
            status_code=422,
            detail=(
                "Cannot generate Form 24Q: missing or invalid 10-char PAN for TDS deductees: "
                f"{', '.join(missing_pan_names)}."
            ),
        )

    fh_line = "^".join(
        [
            "FH",
            "1",
            "24Q",
            _caret_safe(tan_number.upper()),
            _caret_safe(corporate_pan.upper()),
            fy_label,
            ay_label,
            quarter,
            _caret_safe(company_name),
            _caret_safe(registered_address),
            "K",
        ]
    )
    bh_line = "^".join(
        [
            "BH",
            "1",
            _caret_safe(cycle_name),
            pay_dt_str,
            str(sr_no),
            f"{tot_gross:.2f}",
            f"{tot_base:.2f}",
            f"{tot_sur:.2f}",
            f"{tot_cess:.2f}",
            f"{tot_tds:.2f}",
        ]
    )
    cd_line = "^".join(
        [
            "CD",
            "1",
            "192B",
            "200",
            pay_dt_str,
            f"{tot_tds:.2f}",
            f"CHALLAN-{cycle_year}{cycle_month:02d}",
        ]
    )

    lines = [fh_line, bh_line, cd_line, *dd_lines]
    safe_tan = re.sub(r"[^A-Za-z0-9]", "", tan_number or "TAN").upper() or "TAN"
    filename = f"Form24Q_{safe_tan}_{fy_label}_{quarter}_{cycle_year}_{cycle_month:02d}.txt"

    return Form24QExportBundle(
        filename=filename,
        content="\n".join(lines) + "\n",
        financial_year=fy_label,
        assessment_year=ay_label,
        quarter=quarter,
        deductee_count=sr_no,
        total_gross_paid=tot_gross,
        total_tds_deducted=tot_tds,
    )


async def generate_cycle_form24q_export(conn, cycle_id: str) -> Form24QExportBundle:
    cid = validate_uuid(cycle_id, "cycle_id")
    cycle = await conn.fetchrow(
        """
        SELECT id, cycle_name, cycle_month, cycle_year, end_date, status
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
                f"Form 24Q export is available only for APPROVED or DISBURSED cycles "
                f"(current status: '{cycle['status']}')."
            ),
        )

    company = await get_company_statutory_details(conn)
    rows = await conn.fetch(
        """
        SELECT pe.id,
               pe.employee_id,
               u.full_name,
               u.pan_number,
               pe.gross_earnings,
               pe.tds_amount,
               pe.deductions_breakdown
        FROM payroll_entries pe
        JOIN users u ON u.id = pe.employee_id
        WHERE pe.cycle_id = $1::uuid
          AND pe.status = 'CLEARED'
        ORDER BY u.full_name ASC;
        """,
        cid,
    )
    if not rows:
        raise HTTPException(
            status_code=400, detail="No CLEARED entries found for Form 24Q export."
        )

    return build_form24q_content(
        rows,
        company_name=company.company_name,
        tan_number=company.tan_number,
        corporate_pan=company.pan_number,
        registered_address=company.registered_address,
        cycle_name=str(cycle["cycle_name"]),
        cycle_month=int(cycle["cycle_month"]),
        cycle_year=int(cycle["cycle_year"]),
        payment_date=cycle["end_date"],
    )
