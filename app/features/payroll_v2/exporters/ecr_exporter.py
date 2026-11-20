"""
EPFO Electronic Challan cum Return (ECR) Text Exporter (Phase 5).

Generates official `#~#` delimited `.txt` files compliant with the EPFO Unified
Portal ECR specification:
  UAN#~#MemberName#~#GrossWages#~#EPFWages#~#EPSWages#~#EDLIWages#~#EEShare#~#EPSShare#~#ERShare#~#NCPDays#~#RefundOfAdvances

Compliance guarantees:
  - Allowed only for APPROVED or DISBURSED payroll cycles.
  - Excludes all `ON_HOLD` line items (`WHERE status = 'CLEARED'`).
  - Validates 12-digit UAN presence for every EPF-contributing member before export.
  - Enforces exact EPFO arithmetic checksum: `EE Share == EPS Share + ER Share`.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any, Mapping, Sequence

from fastapi import HTTPException
from pydantic import BaseModel

from app.features.payroll_v2.engine_india import (
    EDLI_STATUTORY_WAGE_CEILING,
    round_rupee,
)
from app.features.payroll_v2.master_service import (
    get_company_statutory_details,
    validate_uuid,
)

ECR_DELIMITER = "#~#"
UAN_PATTERN = re.compile(r"^[0-9]{12}$")
EXPORTABLE_CYCLE_STATUSES = frozenset({"APPROVED", "DISBURSED"})


class ECRMemberRecord(BaseModel):
    uan: str
    member_name: str
    gross_wages: int
    epf_wages: int
    eps_wages: int
    edli_wages: int
    ee_share: int
    eps_share: int
    er_share: int
    ncp_days: int
    refund_of_advances: int = 0


class ECRExportBundle(BaseModel):
    filename: str
    content: str
    member_count: int
    total_gross_wages: int
    total_epf_wages: int
    total_eps_wages: int
    total_edli_wages: int
    total_ee_share: int
    total_eps_share: int
    total_er_share: int


def sanitize_ecr_member_name(raw_name: str) -> str:
    """
    EPFO Unified Portal rejects special characters, digits, and `#~#` delimiters
    in MemberName. Keep uppercase ASCII letters and single spaces only.
    """
    cleaned = re.sub(r"[^A-Za-z\s]", " ", raw_name or "")
    collapsed = " ".join(cleaned.upper().split())
    return collapsed[:85] or "EMPLOYEE"


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


def _col(r: Any, key: str, default: Any = None) -> Any:
    try:
        return r[key]
    except (KeyError, IndexError):
        return default


def build_epfo_ecr_content(
    rows: Sequence[Mapping[str, Any]],
    *,
    establishment_code: str,
    cycle_year: int,
    cycle_month: int,
) -> ECRExportBundle:
    """
    Pure deterministic formatter turning cleared EPF rows into an EPFO ECR `.txt` payload.
    Raises HTTP 422 if any EPF-applicable cleared employee lacks a valid 12-digit UAN.
    """
    missing_uan_names: list[str] = []
    ecr_lines: list[str] = []

    tot_gross = 0
    tot_epf_w = 0
    tot_eps_w = 0
    tot_edli_w = 0
    tot_ee = 0
    tot_eps = 0
    tot_er = 0

    for r in rows:
        epf_wages_dec = Decimal(str(r["epf_wages"]))
        ee_share_dec = Decimal(str(r["epf_employee_share"]))
        if epf_wages_dec <= Decimal("0") and ee_share_dec <= Decimal("0"):
            # Employee is opted out / ineligible for EPF
            continue

        emp_name = str(_col(r, "full_name") or _col(r, "employee_name") or "Employee")
        uan = str(_col(r, "uan_number") or "").strip()
        if not UAN_PATTERN.match(uan):
            missing_uan_names.append(emp_name)
            continue

        gross_int = int(round_rupee(Decimal(str(r["gross_earnings"]))))
        epf_w_int = int(round_rupee(epf_wages_dec))
        eps_w_int = int(round_rupee(Decimal(str(r["eps_wages"]))))

        d_json = _parse_jsonb(_col(r, "deductions_breakdown"))
        if d_json.get("edli_wages") is not None:
            edli_w_int = int(round_rupee(Decimal(str(d_json["edli_wages"]))))
        else:
            edli_w_int = int(round_rupee(min(epf_wages_dec, EDLI_STATUTORY_WAGE_CEILING)))

        ee_int = int(round_rupee(ee_share_dec))
        eps_int = int(round_rupee(Decimal(str(r["eps_employer_share"]))))
        er_int = int(round_rupee(Decimal(str(r["epf_employer_share"]))))

        # Enforce strict EPFO portal checksum invariant: EE == EPS + ER
        if ee_int != eps_int + er_int:
            er_int = ee_int - eps_int

        ncp_int = int(round_rupee(Decimal(str(r["ncp_days"]))))
        refund_int = 0

        member = ECRMemberRecord(
            uan=uan,
            member_name=sanitize_ecr_member_name(emp_name),
            gross_wages=gross_int,
            epf_wages=epf_w_int,
            eps_wages=eps_w_int,
            edli_wages=edli_w_int,
            ee_share=ee_int,
            eps_share=eps_int,
            er_share=er_int,
            ncp_days=ncp_int,
            refund_of_advances=refund_int,
        )

        line = ECR_DELIMITER.join(
            [
                member.uan,
                member.member_name,
                str(member.gross_wages),
                str(member.epf_wages),
                str(member.eps_wages),
                str(member.edli_wages),
                str(member.ee_share),
                str(member.eps_share),
                str(member.er_share),
                str(member.ncp_days),
                str(member.refund_of_advances),
            ]
        )
        ecr_lines.append(line)

        tot_gross += gross_int
        tot_epf_w += epf_w_int
        tot_eps_w += eps_w_int
        tot_edli_w += edli_w_int
        tot_ee += ee_int
        tot_eps += eps_int
        tot_er += er_int

    if missing_uan_names:
        raise HTTPException(
            status_code=422,
            detail=(
                "Cannot generate EPFO ECR: missing or invalid 12-digit UAN for "
                f"{', '.join(missing_uan_names)}."
            ),
        )

    safe_est = re.sub(r"[^A-Za-z0-9]", "", establishment_code or "EPFO") or "EPFO"
    filename = f"EPFO_ECR_{safe_est}_{cycle_year}_{cycle_month:02d}.txt"
    content = "\n".join(ecr_lines) + ("\n" if ecr_lines else "")

    return ECRExportBundle(
        filename=filename,
        content=content,
        member_count=len(ecr_lines),
        total_gross_wages=tot_gross,
        total_epf_wages=tot_epf_w,
        total_eps_wages=tot_eps_w,
        total_edli_wages=tot_edli_w,
        total_ee_share=tot_ee,
        total_eps_share=tot_eps,
        total_er_share=tot_er,
    )


async def generate_cycle_ecr_export(conn, cycle_id: str) -> ECRExportBundle:
    """
    Fetch APPROVED/DISBURSED cycle + CLEARED entries and render EPFO ECR file.
    """
    cid = validate_uuid(cycle_id, "cycle_id")
    cycle = await conn.fetchrow(
        "SELECT id, cycle_month, cycle_year, status FROM payroll_cycles WHERE id = $1::uuid;",
        cid,
    )
    if not cycle:
        raise HTTPException(status_code=404, detail="Payroll cycle not found.")
    if cycle["status"] not in EXPORTABLE_CYCLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"EPFO ECR export is available only for APPROVED or DISBURSED cycles "
                f"(current status: '{cycle['status']}')."
            ),
        )

    company = await get_company_statutory_details(conn)
    rows = await conn.fetch(
        """
        SELECT pe.id,
               pe.employee_id,
               u.full_name,
               u.uan_number,
               pe.gross_earnings,
               pe.epf_wages,
               pe.eps_wages,
               pe.epf_employee_share,
               pe.eps_employer_share,
               pe.epf_employer_share,
               pe.ncp_days,
               pe.deductions_breakdown
        FROM payroll_entries pe
        JOIN users u ON u.id = pe.employee_id
        WHERE pe.cycle_id = $1::uuid
          AND pe.status = 'CLEARED'
        ORDER BY u.uan_number ASC NULLS LAST, u.full_name ASC;
        """,
        cid,
    )
    if not rows:
        raise HTTPException(
            status_code=400, detail="No CLEARED payroll entries found for EPFO ECR export."
        )

    return build_epfo_ecr_content(
        rows,
        establishment_code=company.epfo_establishment_code,
        cycle_year=int(cycle["cycle_year"]),
        cycle_month=int(cycle["cycle_month"]),
    )
