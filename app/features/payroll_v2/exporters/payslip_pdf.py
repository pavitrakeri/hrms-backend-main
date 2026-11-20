"""
Branded Indian Statutory Payslip PDF Generator (Phase 5).

Pure-Python vector PDF 1.4 generator using built-in Type1 Helvetica fonts
(zero C-library / GTK / system-binary dependencies; 100% portable on Windows
and Linux/Render). Outputs verified by `pypdf.PdfReader`.

Includes all TDD Section 7.4 statutory sections:
  - Employer Details: Company Name, Registered Address, PAN, TAN, EPFO Est. Code, ESIC Code
  - Employee Details: Name, Designation, Department, Joining Date, Bank Account, IFSC, PAN, UAN, ESIC IP
  - Attendance Bar: Calendar Days, Paid / Worked Days, Unpaid LOP Days, Absent Days, NCP Days
  - Side-by-Side Earnings & Deductions Table (incl. VPF & Approved Reimbursements)
  - Net Take-Home Pay in Figures (Indian numbering e.g. Rs. 1,25,000.00) & Indian Words (Lakhs/Crores)
  - Employer Statutory Contributions & Gratuity Accrual Summary
"""

from __future__ import annotations

import calendar
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping, Optional

from fastapi import HTTPException
from pydantic import BaseModel

from app.features.payroll_v2.engine_india import round_paisa
from app.features.payroll_v2.master_service import (
    get_company_statutory_details,
    validate_uuid,
)

EXPORTABLE_CYCLE_STATUSES = frozenset({"APPROVED", "DISBURSED"})
PAGE_WIDTH = 595.0
PAGE_HEIGHT = 842.0


class PayslipPDFBundle(BaseModel):
    filename: str
    pdf_bytes: bytes
    employee_name: str
    net_payable: Decimal


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


def format_inr(amount: Decimal | float | int) -> str:
    """Format number in Indian comma grouping: 1234567.89 -> 'Rs. 12,34,567.89'."""
    val = round_paisa(Decimal(str(amount)))
    neg = val < Decimal("0")
    val = abs(val)
    whole_str, dec_str = f"{val:.2f}".split(".")
    if len(whole_str) > 3:
        last3 = whole_str[-3:]
        rest = whole_str[:-3]
        groups: list[str] = []
        while len(rest) > 2:
            groups.append(rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.append(rest)
        grouped = ",".join(reversed(groups)) + "," + last3
    else:
        grouped = whole_str
    sign = "-" if neg else ""
    return f"{sign}Rs. {grouped}.{dec_str}"


_ONES = (
    "",
    "One",
    "Two",
    "Three",
    "Four",
    "Five",
    "Six",
    "Seven",
    "Eight",
    "Nine",
    "Ten",
    "Eleven",
    "Twelve",
    "Thirteen",
    "Fourteen",
    "Fifteen",
    "Sixteen",
    "Seventeen",
    "Eighteen",
    "Nineteen",
)
_TENS = (
    "",
    "",
    "Twenty",
    "Thirty",
    "Forty",
    "Fifty",
    "Sixty",
    "Seventy",
    "Eighty",
    "Ninety",
)


def _two_digits_words(n: int) -> str:
    if n < 20:
        return _ONES[n]
    return (_TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else "")).strip()


def _three_digits_words(n: int) -> str:
    parts: list[str] = []
    if n >= 100:
        parts.append(f"{_ONES[n // 100]} Hundred")
        n %= 100
    if n > 0:
        parts.append(_two_digits_words(n))
    return " ".join(parts)


def amount_to_indian_words(amount: Decimal | float | int) -> str:
    """Convert INR Decimal amount into Indian numbering words (Crores, Lakhs, Thousands)."""
    val = round_paisa(Decimal(str(amount)))
    if val < Decimal("0"):
        return f"Minus {amount_to_indian_words(abs(val))}"

    rupees = int(val)
    paise = int(round((val - Decimal(rupees)) * Decimal("100")))

    if rupees == 0 and paise == 0:
        return "Rupees Zero Only"

    parts: list[str] = []
    crores = rupees // 10_000_000
    rupees %= 10_000_000
    lakhs = rupees // 100_000
    rupees %= 100_000
    thousands = rupees // 1_000
    hundreds_rem = rupees % 1_000

    if crores:
        parts.append(f"{_three_digits_words(crores)} Crore")
    if lakhs:
        parts.append(f"{_two_digits_words(lakhs)} Lakh")
    if thousands:
        parts.append(f"{_two_digits_words(thousands)} Thousand")
    if hundreds_rem:
        parts.append(_three_digits_words(hundreds_rem))

    words = f"Rupees {' '.join(parts)}" if parts else "Rupees Zero"
    if paise:
        words += f" And {_two_digits_words(paise)} Paise"
    return f"{words} Only"


def _pdf_escape(text: str) -> str:
    """Escape text for WinAnsi / Latin-1 Type1 Helvetica strings in PDF content streams."""
    ascii_safe = (
        (text or "")
        .replace("₹", "Rs. ")
        .replace("—", "-")
        .replace("–", "-")
        .encode("latin-1", "replace")
        .decode("latin-1")
    )
    return ascii_safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


class _PDFCanvas:
    """Minimal vector drawing helper using top-down A4 coordinates."""

    def __init__(self) -> None:
        self.ops: list[str] = []

    def _y(self, top_y: float) -> float:
        return PAGE_HEIGHT - top_y

    def rect(
        self,
        x: float,
        top_y: float,
        w: float,
        h: float,
        *,
        fill_rgb: Optional[tuple[float, float, float]] = None,
        stroke_rgb: Optional[tuple[float, float, float]] = None,
        line_width: float = 0.6,
    ) -> None:
        bottom_y = self._y(top_y + h)
        self.ops.append("q")
        if fill_rgb:
            self.ops.append(f"{fill_rgb[0]:.3f} {fill_rgb[1]:.3f} {fill_rgb[2]:.3f} rg")
        if stroke_rgb:
            self.ops.append(f"{line_width:.2f} w")
            self.ops.append(f"{stroke_rgb[0]:.3f} {stroke_rgb[1]:.3f} {stroke_rgb[2]:.3f} RG")
        paint = "B" if (fill_rgb and stroke_rgb) else ("f" if fill_rgb else "S")
        self.ops.append(f"{x:.2f} {bottom_y:.2f} {w:.2f} {h:.2f} re {paint}")
        self.ops.append("Q")

    def line(
        self,
        x1: float,
        top_y1: float,
        x2: float,
        top_y2: float,
        *,
        rgb: tuple[float, float, float] = (0.82, 0.85, 0.89),
        width: float = 0.6,
    ) -> None:
        self.ops.append("q")
        self.ops.append(f"{width:.2f} w {rgb[0]:.3f} {rgb[1]:.3f} {rgb[2]:.3f} RG")
        self.ops.append(f"{x1:.2f} {self._y(top_y1):.2f} m {x2:.2f} {self._y(top_y2):.2f} l S")
        self.ops.append("Q")

    def text(
        self,
        x: float,
        top_y: float,
        content: str,
        *,
        size: float = 9.0,
        bold: bool = False,
        rgb: tuple[float, float, float] = (0.10, 0.13, 0.19),
    ) -> None:
        font = "F2" if bold else "F1"
        self.ops.append("q")
        self.ops.append(f"{rgb[0]:.3f} {rgb[1]:.3f} {rgb[2]:.3f} rg")
        self.ops.append(f"BT /{font} {size:.1f} Tf 1 0 0 1 {x:.2f} {self._y(top_y):.2f} Tm ({_pdf_escape(content)}) Tj ET")
        self.ops.append("Q")

    def build_pdf(self) -> bytes:
        stream_bytes = "\n".join(self.ops).encode("latin-1")

        objects: list[bytes] = [
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
            (
                b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                b"/Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>\nendobj\n"
            ),
            b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>\nendobj\n",
            b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>\nendobj\n",
            (
                f"6 0 obj\n<< /Length {len(stream_bytes)} >>\nstream\n".encode("latin-1")
                + stream_bytes
                + b"\nendstream\nendobj\n"
            ),
        ]

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets: list[int] = [0]
        for obj in objects:
            offsets.append(len(out))
            out.extend(obj)

        xref_start = len(out)
        out.extend(f"xref\n0 {len(objects) + 1}\n".encode("latin-1"))
        out.extend(b"0000000000 65535 f \n")
        for off in offsets[1:]:
            out.extend(f"{off:010d} 00000 n \n".encode("latin-1"))

        out.extend(
            (
                f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
                f"startxref\n{xref_start}\n%%EOF\n"
            ).encode("latin-1")
        )
        return bytes(out)


def render_indian_payslip_pdf(
    *,
    company: Mapping[str, Any],
    employee: Mapping[str, Any],
    cycle: Mapping[str, Any],
    entry: Mapping[str, Any],
    bank: Optional[Mapping[str, Any]] = None,
) -> PayslipPDFBundle:
    """
    Pure PDF renderer producing A4 statutory payslip bytes from immutable snapshot records.
    """
    canvas = _PDFCanvas()
    margin = 36.0
    content_w = PAGE_WIDTH - 2 * margin  # 523.0

    month_num = int(cycle["cycle_month"])
    year_num = int(cycle["cycle_year"])
    month_label = f"{calendar.month_name[month_num].upper()} {year_num}"
    emp_name = str(_col(employee, "full_name") or _col(employee, "employee_name") or "Employee")

    e_json = _parse_jsonb(_col(entry, "earnings_breakdown"))
    d_json = _parse_jsonb(_col(entry, "deductions_breakdown"))
    tb = d_json.get("tax_breakdown") if isinstance(d_json.get("tax_breakdown"), dict) else {}

    # 1. Corporate Navy Banner Header
    canvas.rect(margin, 32, content_w, 86, fill_rgb=(0.07, 0.14, 0.28))
    canvas.text(
        margin + 14,
        54,
        str(_col(company, "company_name") or "Aimploy Technologies India Private Limited"),
        size=13.5,
        bold=True,
        rgb=(1.0, 1.0, 1.0),
    )
    addr = str(_col(company, "registered_address") or "")
    canvas.text(margin + 14, 70, addr[:92], size=8.0, rgb=(0.82, 0.87, 0.96))
    canvas.text(
        margin + 14,
        85,
        (
            f"PAN: {_col(company, 'pan_number') or '-'}   |   "
            f"TAN: {_col(company, 'tan_number') or '-'}   |   "
            f"EPFO Est: {_col(company, 'epfo_establishment_code') or '-'}"
        ),
        size=8.0,
        rgb=(0.88, 0.92, 0.99),
    )
    canvas.text(
        margin + 14,
        104,
        f"PAYSLIP FOR {month_label}   ({str(cycle['cycle_name'])})",
        size=10.5,
        bold=True,
        rgb=(0.55, 0.85, 0.99),
    )

    # 2. Employee & Statutory Identifiers Grid
    y = 130.0
    canvas.rect(
        margin,
        y,
        content_w,
        118,
        fill_rgb=(0.98, 0.99, 1.0),
        stroke_rgb=(0.80, 0.84, 0.90),
    )
    canvas.text(margin + 12, y + 16, "EMPLOYEE & STATUTORY DETAILS", size=8.5, bold=True, rgb=(0.15, 0.25, 0.45))
    canvas.line(margin, y + 23, margin + content_w, y + 23)

    joining = _col(employee, "joining_date")
    joining_str = joining.isoformat() if isinstance(joining, (date, datetime)) else (str(joining) if joining else "-")
    bank_name = str(_col(bank, "bank_name") or "-") if bank else "-"
    acct_no = str(_col(bank, "account_number") or "-") if bank else "-"
    ifsc = str(_col(bank, "ifsc_code") or "-") if bank else "-"
    regime = str(tb.get("regime") or d_json.get("tax_regime") or "NEW")

    left_meta = [
        ("Employee Name", emp_name),
        ("Designation", str(_col(employee, "designation") or "-")),
        ("Department", str(_col(employee, "department") or "-")),
        ("Date of Joining", joining_str),
        ("Tax Regime", f"{regime} REGIME (Sec 115BAC)" if regime == "NEW" else "OLD REGIME"),
    ]
    right_meta = [
        ("Employee PAN", str(_col(employee, "pan_number") or "Not Provided")),
        ("EPFO UAN", str(_col(employee, "uan_number") or "Not Provided")),
        ("ESIC IP No.", str(_col(employee, "esic_ip_number") or "N/A")),
        ("Bank Name", bank_name[:28]),
        ("A/C & IFSC", f"{acct_no} / {ifsc}"),
    ]

    row_y = y + 38
    for (lk, lv), (rk, rv) in zip(left_meta, right_meta):
        canvas.text(margin + 12, row_y, f"{lk}:", size=8.2, bold=True, rgb=(0.35, 0.40, 0.48))
        canvas.text(margin + 100, row_y, lv[:28], size=8.5)
        canvas.text(margin + 275, row_y, f"{rk}:", size=8.2, bold=True, rgb=(0.35, 0.40, 0.48))
        canvas.text(margin + 355, row_y, rv[:28], size=8.5)
        row_y += 16

    # 3. Attendance & NCP Strip
    y = 258.0
    canvas.rect(
        margin,
        y,
        content_w,
        40,
        fill_rgb=(0.94, 0.96, 0.99),
        stroke_rgb=(0.80, 0.84, 0.90),
    )
    att_cols = [
        ("Calendar Days", str(int(entry["total_calendar_days"]))),
        ("Paid / Worked Days", f"{Decimal(str(entry['days_worked'])):.1f}"),
        ("Unpaid LOP", f"{Decimal(str(entry['unpaid_leave_days'])):.1f}"),
        ("Absent Days", f"{Decimal(str(entry['unexcused_absent_days'])):.1f}"),
        ("EPFO NCP Days", f"{Decimal(str(entry['ncp_days'])):.1f}"),
    ]
    col_step = content_w / len(att_cols)
    for idx, (lbl, val) in enumerate(att_cols):
        cx = margin + 12 + idx * col_step
        canvas.text(cx, y + 15, lbl, size=7.5, bold=True, rgb=(0.35, 0.40, 0.48))
        canvas.text(cx, y + 30, val, size=9.5, bold=True)

    # 4. Side-by-Side Earnings & Deductions Financial Table
    y = 310.0
    half_w = content_w / 2.0
    table_h = 206.0
    canvas.rect(margin, y, content_w, table_h, stroke_rgb=(0.75, 0.80, 0.87))
    canvas.rect(margin, y, content_w, 24, fill_rgb=(0.12, 0.22, 0.39))
    canvas.line(margin + half_w, y, margin + half_w, y + table_h, rgb=(0.75, 0.80, 0.87), width=0.8)

    canvas.text(margin + 12, y + 16, "EARNINGS", size=9.0, bold=True, rgb=(1.0, 1.0, 1.0))
    canvas.text(margin + 185, y + 16, "AMOUNT", size=9.0, bold=True, rgb=(1.0, 1.0, 1.0))
    canvas.text(margin + half_w + 12, y + 16, "DEDUCTIONS", size=9.0, bold=True, rgb=(1.0, 1.0, 1.0))
    canvas.text(margin + half_w + 185, y + 16, "AMOUNT", size=9.0, bold=True, rgb=(1.0, 1.0, 1.0))

    earned_basic = round_paisa(Decimal(str(entry["earned_basic"])))
    earned_hra = round_paisa(Decimal(str(entry["earned_hra"])))
    earned_spec = round_paisa(Decimal(str(entry["earned_special_allowance"])))
    earned_da = round_paisa(Decimal(str(entry["earned_da"])))
    reimbursements = round_paisa(Decimal(str(entry["reimbursements_amount"])))
    gross_earnings = round_paisa(Decimal(str(entry["gross_earnings"])))
    total_earnings_with_reim = round_paisa(gross_earnings + reimbursements)

    epf_ee = round_paisa(Decimal(str(entry["epf_employee_share"])))
    vpf_ee = round_paisa(Decimal(str(d_json.get("vpf_employee") or "0")))
    esi_ee = round_paisa(Decimal(str(entry["esi_employee_share"])))
    pt_amt = round_paisa(Decimal(str(entry["professional_tax"])))
    tds_amt = round_paisa(Decimal(str(entry["tds_amount"])))
    adv_amt = round_paisa(Decimal(str(entry["advance_recovery_amount"])))
    total_deductions = round_paisa(epf_ee + vpf_ee + esi_ee + pt_amt + tds_amt + adv_amt)
    net_payable = round_paisa(Decimal(str(entry["net_payable"])))

    earnings_rows = [
        ("Basic Salary", format_inr(earned_basic)),
        ("House Rent Allowance (HRA)", format_inr(earned_hra)),
        ("Special Allowance", format_inr(earned_spec)),
        ("Dearness Allowance (DA)", format_inr(earned_da)),
        ("Approved Reimbursements", format_inr(reimbursements)),
    ]
    deduction_rows = [
        ("Employee EPF (12%)", format_inr(epf_ee)),
        ("Voluntary PF (VPF)", format_inr(vpf_ee)),
        ("Employee ESIC (0.75%)", format_inr(esi_ee)),
        ("Professional Tax (PT)", format_inr(pt_amt)),
        ("Income Tax TDS (Sec 192)", format_inr(tds_amt)),
        ("Salary Advance EMI", format_inr(adv_amt)),
    ]

    fy = y + 44
    for lbl, val in earnings_rows:
        canvas.text(margin + 12, fy, lbl, size=8.5)
        canvas.text(margin + 178, fy, val, size=8.5)
        fy += 22

    dy = y + 44
    for lbl, val in deduction_rows:
        canvas.text(margin + half_w + 12, dy, lbl, size=8.5)
        canvas.text(margin + half_w + 178, dy, val, size=8.5)
        dy += 22

    # Totals Row inside Table
    tot_y = y + table_h - 30
    canvas.rect(margin, tot_y, content_w, 30, fill_rgb=(0.95, 0.97, 1.0), stroke_rgb=(0.75, 0.80, 0.87))
    canvas.line(margin + half_w, tot_y, margin + half_w, tot_y + 30, rgb=(0.75, 0.80, 0.87), width=0.8)
    canvas.text(margin + 12, tot_y + 19, "Gross Earnings + Reimb.", size=8.5, bold=True)
    canvas.text(margin + 175, tot_y + 19, format_inr(total_earnings_with_reim), size=8.8, bold=True)
    canvas.text(margin + half_w + 12, tot_y + 19, "Total Deductions", size=8.5, bold=True)
    canvas.text(margin + half_w + 175, tot_y + 19, format_inr(total_deductions), size=8.8, bold=True)

    # 5. Net Take-Home Pay Box (Figures & Words)
    y = 530.0
    canvas.rect(
        margin,
        y,
        content_w,
        62,
        fill_rgb=(0.91, 0.97, 0.93),
        stroke_rgb=(0.20, 0.56, 0.36),
        line_width=1.0,
    )
    canvas.text(margin + 14, y + 22, "NET TAKE-HOME PAYABLE:", size=10.0, bold=True, rgb=(0.08, 0.35, 0.20))
    canvas.text(margin + 190, y + 22, format_inr(net_payable), size=13.0, bold=True, rgb=(0.06, 0.30, 0.16))
    words_line = f"In Words: {amount_to_indian_words(net_payable)}"
    canvas.text(margin + 14, y + 43, words_line[:94], size=8.5, bold=True, rgb=(0.15, 0.35, 0.23))

    # 6. Employer Statutory Contributions & Gratuity Liability Box
    y = 606.0
    canvas.rect(margin, y, content_w, 72, fill_rgb=(0.98, 0.99, 1.0), stroke_rgb=(0.82, 0.85, 0.90))
    canvas.text(margin + 12, y + 16, "EMPLOYER STATUTORY CONTRIBUTIONS & COST SUMMARY (INFORMATIONAL)", size=8.0, bold=True, rgb=(0.30, 0.36, 0.45))
    canvas.line(margin, y + 23, margin + content_w, y + 23)

    er_pf = round_paisa(Decimal(str(entry["epf_employer_share"])))
    er_eps = round_paisa(Decimal(str(entry["eps_employer_share"])))
    er_edli = round_paisa(Decimal(str(entry["edli_employer_share"])))
    er_admin = round_paisa(Decimal(str(entry["epf_admin_charges"])))
    er_esi = round_paisa(Decimal(str(entry["esi_employer_share"])))
    grat = round_paisa(Decimal(str(entry["gratuity_accrual_monthly"])))
    total_ctc_mo = round_paisa(Decimal(str(entry["total_employer_cost"])))

    canvas.text(
        margin + 12,
        y + 40,
        f"ER EPF (3.67%): {format_inr(er_pf)}   |   ER EPS (8.33%): {format_inr(er_eps)}   |   EDLI+Admin: {format_inr(er_edli + er_admin)}",
        size=8.0,
    )
    canvas.text(
        margin + 12,
        y + 58,
        f"ER ESIC (3.25%): {format_inr(er_esi)}   |   Gratuity Reserve (15/26): {format_inr(grat)}   |   Total CTC: {format_inr(total_ctc_mo)}",
        size=8.0,
        bold=True,
    )

    # 7. Footer & Disbursement Metadata
    utr = str(_col(entry, "bank_utr_number") or "Pending Bank Release")
    payout_st = str(_col(entry, "payout_status") or "PENDING")
    canvas.line(margin, 706, margin + content_w, 706)
    canvas.text(margin + 8, 724, f"Payout Status: {payout_st}   |   Bank UTR / Reference: {utr}", size=8.0, bold=True)
    canvas.text(
        margin + 8,
        742,
        "This is a computer-generated Indian statutory payslip issued by Aimploy HRMS Payroll V2.",
        size=7.8,
        rgb=(0.45, 0.50, 0.58),
    )

    safe_emp = "_".join(emp_name.strip().split())[:40] or "Employee"
    filename = f"Payslip_{safe_emp}_{year_num}_{month_num:02d}.pdf"
    return PayslipPDFBundle(
        filename=filename,
        pdf_bytes=canvas.build_pdf(),
        employee_name=emp_name,
        net_payable=net_payable,
    )


async def generate_payslip_pdf_for_employee(
    conn,
    *,
    entry_id: str,
    requester_employee_id: Optional[str] = None,
    cycle_id: Optional[str] = None,
) -> PayslipPDFBundle:
    """
    Fetch entry + cycle + employee + company + primary bank account and render PDF.
    Enforces:
      - Cycle status MUST be APPROVED or DISBURSED.
      - Entry MUST be CLEARED (held records have no official payslip).
      - When called from `/me/...`, strictly enforces `employee_id == requester_employee_id`.
    """
    eid = validate_uuid(entry_id, "entry_id")
    conditions = ["pe.id = $1::uuid", "pe.status = 'CLEARED'", "pc.status IN ('APPROVED', 'DISBURSED')"]
    params: list[Any] = [eid]
    idx = 2

    if requester_employee_id is not None:
        emp_uuid = validate_uuid(requester_employee_id, "employee_id")
        conditions.append(f"pe.employee_id = ${idx}::uuid")
        params.append(emp_uuid)
        idx += 1

    if cycle_id is not None:
        cid = validate_uuid(cycle_id, "cycle_id")
        conditions.append(f"pe.cycle_id = ${idx}::uuid")
        params.append(cid)
        idx += 1

    row = await conn.fetchrow(
        f"""
        SELECT pe.*,
               pc.cycle_name,
               pc.cycle_month,
               pc.cycle_year,
               pc.status AS cycle_status,
               u.full_name,
               u.email,
               u.designation,
               u.joining_date,
               u.pan_number,
               u.uan_number,
               u.esic_ip_number,
               d.name AS department,
               ebd.bank_name,
               ebd.account_number,
               ebd.ifsc_code
        FROM payroll_entries pe
        JOIN payroll_cycles pc ON pc.id = pe.cycle_id
        JOIN users u ON u.id = pe.employee_id
        LEFT JOIN departments d ON d.id = u.department_id
        LEFT JOIN employee_bank_details ebd
               ON ebd.employee_id = pe.employee_id
              AND ebd.is_primary = true
        WHERE {' AND '.join(conditions)};
        """,
        *params,
    )
    if not row:
        raise HTTPException(
            status_code=404,
            detail="Finalized CLEARED payslip not found (cycle must be APPROVED or DISBURSED).",
        )

    company = await get_company_statutory_details(conn)
    bank_map = {
        "bank_name": _col(row, "bank_name"),
        "account_number": _col(row, "account_number"),
        "ifsc_code": _col(row, "ifsc_code"),
    }
    return render_indian_payslip_pdf(
        company=company.model_dump(),
        employee=row,
        cycle=row,
        entry=row,
        bank=bank_map,
    )
