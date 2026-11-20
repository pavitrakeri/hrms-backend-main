"""
Statutory Compliance & Payslip PDF Exporters (Phase 5) for Payroll V2.

Modules:
  - ecr_exporter.py     : EPFO Electronic Challan cum Return (`#~#` delimited `.txt`)
  - bank_exporter.py    : Corporate Bank NEFT/RTGS Bulk Payout `.csv`
  - form24q_exporter.py : Income Tax Form 24Q e-TDS (`^` delimited `.txt`)
  - payslip_pdf.py      : Pure-Python Branded A4 Indian Statutory Payslip `.pdf`
"""

from app.features.payroll_v2.exporters.bank_exporter import (
    BankExportBundle,
    build_bank_neft_csv,
    generate_cycle_bank_export,
)
from app.features.payroll_v2.exporters.ecr_exporter import (
    ECRExportBundle,
    ECRMemberRecord,
    build_epfo_ecr_content,
    generate_cycle_ecr_export,
    sanitize_ecr_member_name,
)
from app.features.payroll_v2.exporters.form24q_exporter import (
    Form24QExportBundle,
    build_form24q_content,
    generate_cycle_form24q_export,
    get_tax_quarter_and_years,
)
from app.features.payroll_v2.exporters.payslip_pdf import (
    PayslipPDFBundle,
    amount_to_indian_words,
    format_inr,
    generate_payslip_pdf_for_employee,
    render_indian_payslip_pdf,
)

__all__ = [
    "BankExportBundle",
    "ECRExportBundle",
    "ECRMemberRecord",
    "Form24QExportBundle",
    "PayslipPDFBundle",
    "amount_to_indian_words",
    "build_bank_neft_csv",
    "build_epfo_ecr_content",
    "build_form24q_content",
    "format_inr",
    "generate_cycle_bank_export",
    "generate_cycle_ecr_export",
    "generate_cycle_form24q_export",
    "generate_payslip_pdf_for_employee",
    "get_tax_quarter_and_years",
    "render_indian_payslip_pdf",
    "sanitize_ecr_member_name",
]
