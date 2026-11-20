"""
Pydantic v2 Data Models for Payroll V2 (India Statutory Payroll).

Strictly synchronized with migrations/015_india_payroll_schema.sql.
All monetary input fields use Decimal for precision; API response DTOs expose
clean JSON-friendly numbers and ISO timestamps.
"""

from __future__ import annotations

import calendar
import re
from datetime import date
from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

IFSC_REGEX = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
PAN_REGEX = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
TAN_REGEX = re.compile(r"^[A-Z]{4}[0-9]{5}[A-Z]$")
UAN_REGEX = re.compile(r"^[0-9]{12}$")
AADHAAR_REGEX = re.compile(r"^[0-9]{12}$")
FY_REGEX = re.compile(r"^(\d{4})-(\d{4})$")


# ---------------------------------------------------------------------------
# Enumerations matching 015_india_payroll_schema.sql CHECK constraints
# ---------------------------------------------------------------------------
CycleType = Literal["REGULAR", "OFF_CYCLE", "SUPPLEMENTAL"]
CycleStatus = Literal[
    "DRAFT",
    "CALCULATING",
    "REVIEW",
    "APPROVED",
    "DISBURSEMENT_IN_PROGRESS",
    "PARTIALLY_DISBURSED",
    "DISBURSED",
    "LOCKED",
    "REJECTED",
]
EntryStatus = Literal["CLEARED", "ON_HOLD"]
PayoutStatus = Literal[
    "NOT_GENERATED",
    "GENERATED",
    "SUBMITTED_TO_BANK",
    "BANK_ACCEPTED",
    "PROCESSING",
    "PAID",
    "FAILED",
    "REVERSED",
    "REQUIRES_REVIEW",
    "PENDING",
]
TaxRegime = Literal["NEW", "OLD"]
PaymentMethod = Literal["BANK_TRANSFER", "CHEQUE", "CASH"]
AdvanceStatus = Literal["ACTIVE", "COMPLETED", "CANCELLED", "PAUSED"]
ApprovalDecision = Literal["APPROVED", "REJECTED"]


# ---------------------------------------------------------------------------
# Master Setup DTOs (Company, PT Slabs, Statutory KYC, Salary, Bank, Advance, Tax)
# ---------------------------------------------------------------------------
class CompanyStatutoryUpsertRequest(BaseModel):
    company_name: str = Field(..., min_length=2, max_length=250)
    tan_number: str = Field(..., min_length=10, max_length=10)
    pan_number: str = Field(..., min_length=10, max_length=10)
    epfo_establishment_code: str = Field(..., min_length=5, max_length=15)
    esic_establishment_code: Optional[str] = Field(default=None, max_length=17)
    is_esic_applicable: bool = Field(default=False)
    pt_rc_number: Optional[str] = Field(default=None, max_length=100)
    pt_ec_number: Optional[str] = Field(default=None, max_length=100)
    registered_address: str = Field(..., min_length=5, max_length=500)

    @field_validator("tan_number")
    @classmethod
    def _val_tan(cls, v: str) -> str:
        val = v.strip().upper()
        if not TAN_REGEX.match(val):
            raise ValueError("Invalid TAN format. Expected 10 chars e.g. BLRA12345C.")
        return val

    @field_validator("pan_number")
    @classmethod
    def _val_pan(cls, v: str) -> str:
        val = v.strip().upper()
        if not PAN_REGEX.match(val):
            raise ValueError("Invalid Corporate PAN format. Expected 10 chars e.g. AABCA1234F.")
        return val

    @field_validator("epfo_establishment_code")
    @classmethod
    def _val_epfo(cls, v: str) -> str:
        val = v.strip().upper()
        if not val:
            raise ValueError("EPFO establishment code cannot be blank.")
        return val


class CompanyStatutoryResponse(BaseModel):
    id: str
    company_name: str
    tan_number: str
    pan_number: str
    epfo_establishment_code: str
    esic_establishment_code: Optional[str] = None
    is_esic_applicable: bool = False
    pt_rc_number: Optional[str] = None
    pt_ec_number: Optional[str] = None
    registered_address: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class PTSlabItem(BaseModel):
    id: Optional[int] = None
    state_code: str
    state_name: str
    effective_from: date
    min_monthly_gross: float
    max_monthly_gross: Optional[float] = None
    pt_amount: float
    special_feb_amount: Optional[float] = None


class EmployeeStatutoryProfileUpdate(BaseModel):
    pan_number: Optional[str] = Field(default=None, max_length=10)
    uan_number: Optional[str] = Field(default=None, max_length=12)
    aadhaar_number: Optional[str] = Field(default=None, max_length=12)
    esic_ip_number: Optional[str] = Field(default=None, max_length=10)
    father_or_spouse_name: Optional[str] = Field(default=None, max_length=200)
    date_of_exit: Optional[date] = None
    exit_reason_code: Optional[str] = Field(default=None, max_length=50)

    @field_validator("pan_number")
    @classmethod
    def _val_emp_pan(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v.strip() == "":
            return None
        val = v.strip().upper()
        if not PAN_REGEX.match(val):
            raise ValueError("Invalid PAN format (expected e.g. ABCDE1234F).")
        return val

    @field_validator("uan_number")
    @classmethod
    def _val_uan(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v.strip() == "":
            return None
        val = v.strip()
        if not UAN_REGEX.match(val):
            raise ValueError("Invalid UAN format (must be strictly 12 digits).")
        return val

    @field_validator("aadhaar_number")
    @classmethod
    def _val_aadhaar(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v.strip() == "":
            return None
        val = v.strip()
        if not AADHAAR_REGEX.match(val):
            raise ValueError("Invalid Aadhaar format (must be strictly 12 digits).")
        return val

    @field_validator("esic_ip_number")
    @classmethod
    def _val_esic(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v.strip() == "":
            return None
        val = v.strip()
        if len(val) > 10:
            raise ValueError("ESIC IP number cannot exceed 10 characters.")
        return val


class SalaryStructureUpsertRequest(BaseModel):
    employee_id: str
    effective_from: date
    base_currency: str = Field(default="INR", max_length=5)
    annual_ctc: Decimal = Field(..., ge=Decimal("0"))
    monthly_basic_pay: Decimal = Field(..., ge=Decimal("0"))
    monthly_hra: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    monthly_special_allowance: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    monthly_da: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    is_epf_eligible: bool = True
    epf_wage_cap_applied: bool = True
    vpf_percentage: Decimal = Field(default=Decimal("0"), ge=Decimal("0"), le=Decimal("100"))
    is_esi_eligible: Optional[bool] = None
    pt_state_code: str = Field(default="KA", min_length=2, max_length=5)
    tax_regime: TaxRegime = "NEW"

    @field_validator("pt_state_code")
    @classmethod
    def _val_state_code(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("base_currency")
    @classmethod
    def _val_currency(cls, v: str) -> str:
        val = v.strip().upper()
        if val != "INR":
            raise ValueError("Payroll V2 only supports 'INR' as base_currency.")
        return val

    @model_validator(mode="after")
    def _resolve_defaults_and_sanity(self) -> SalaryStructureUpsertRequest:
        monthly_gross = (
            self.monthly_basic_pay
            + self.monthly_hra
            + self.monthly_special_allowance
            + self.monthly_da
        )
        if monthly_gross <= Decimal("0") and self.annual_ctc > Decimal("0"):
            raise ValueError(
                "Sum of monthly earnings components must exceed 0 when annual_ctc > 0."
            )
        if self.annual_ctc < monthly_gross * Decimal("12"):
            raise ValueError(
                "annual_ctc cannot be less than 12 x monthly gross salary."
            )
        if self.is_esi_eligible is None:
            self.is_esi_eligible = monthly_gross <= Decimal("21000")
        return self


class SalaryStructureResponse(BaseModel):
    id: str
    employee_id: str
    employee_name: Optional[str] = None
    email: Optional[str] = None
    effective_from: str
    effective_to: Optional[str] = None
    is_active: bool
    base_currency: str
    annual_ctc: float
    monthly_basic_pay: float
    monthly_hra: float
    monthly_special_allowance: float
    monthly_da: float
    monthly_gross: float
    is_epf_eligible: bool
    epf_wage_cap_applied: bool
    vpf_percentage: float
    is_esi_eligible: bool
    pt_state_code: str
    tax_regime: TaxRegime
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class BankDetailsUpsertRequest(BaseModel):
    payment_method: PaymentMethod = "BANK_TRANSFER"
    account_holder_name: str = Field(..., min_length=2, max_length=200)
    bank_name: str = Field(..., min_length=2, max_length=200)
    account_number: str = Field(..., min_length=4, max_length=40)
    ifsc_code: str = Field(..., min_length=3, max_length=11)
    is_primary: bool = True

    @model_validator(mode="after")
    def _val_ifsc(self) -> BankDetailsUpsertRequest:
        self.ifsc_code = self.ifsc_code.strip().upper()
        self.account_number = self.account_number.strip()
        if self.payment_method == "BANK_TRANSFER":
            if not IFSC_REGEX.match(self.ifsc_code):
                raise ValueError(
                    "Invalid Indian IFSC code. Expected 11 characters e.g. HDFC0000240."
                )
        return self


class BankDetailsResponse(BaseModel):
    id: str
    employee_id: str
    payment_method: PaymentMethod
    account_holder_name: str
    bank_name: str
    account_number: str
    ifsc_code: str
    is_primary: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class TaxDeclarationUpsertRequest(BaseModel):
    financial_year: str = Field(..., min_length=9, max_length=10)
    regime: TaxRegime = "NEW"
    section_80c_amount: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    section_80d_amount: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    annual_rent_paid: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    home_loan_interest: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    nps_80ccd_1b: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))

    @field_validator("financial_year")
    @classmethod
    def _val_fy(cls, v: str) -> str:
        val = v.strip()
        m = FY_REGEX.match(val)
        if not m or int(m.group(2)) != int(m.group(1)) + 1:
            raise ValueError("Invalid financial_year format. Expected e.g. '2026-2027'.")
        return val


class TaxDeclarationVerifyRequest(BaseModel):
    is_verified: bool


class TaxDeclarationResponse(BaseModel):
    id: str
    employee_id: str
    employee_name: Optional[str] = None
    financial_year: str
    regime: TaxRegime
    section_80c_amount: float
    section_80d_amount: float
    annual_rent_paid: float
    home_loan_interest: float
    nps_80ccd_1b: float
    is_verified: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class SalaryAdvanceCreateRequest(BaseModel):
    employee_id: str
    principal_amount: Decimal = Field(..., gt=Decimal("0"))
    monthly_emi_amount: Decimal = Field(..., gt=Decimal("0"))
    total_installments: Optional[int] = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _validate_advance(self) -> SalaryAdvanceCreateRequest:
        if self.monthly_emi_amount > self.principal_amount:
            raise ValueError("monthly_emi_amount cannot exceed principal_amount.")
        if self.total_installments is None:
            # Compute ceil(principal / emi) cleanly without float precision issues
            q, rem = divmod(self.principal_amount, self.monthly_emi_amount)
            self.total_installments = int(q) + (1 if rem > 0 else 0)
        return self


class SalaryAdvanceStatusUpdateRequest(BaseModel):
    status: Literal["ACTIVE", "PAUSED", "CANCELLED"]


class SalaryAdvanceResponse(BaseModel):
    id: str
    employee_id: str
    employee_name: Optional[str] = None
    principal_amount: float
    monthly_emi_amount: float
    total_installments: int
    paid_installments: int
    remaining_balance: float
    status: AdvanceStatus
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class EmployeeStatutoryOverviewRow(BaseModel):
    employee_id: str
    full_name: str
    email: str
    department: Optional[str] = None
    designation: Optional[str] = None
    joining_date: Optional[str] = None
    date_of_exit: Optional[str] = None
    pan_number: Optional[str] = None
    uan_number: Optional[str] = None
    aadhaar_number: Optional[str] = None
    esic_ip_number: Optional[str] = None
    father_or_spouse_name: Optional[str] = None
    exit_reason_code: Optional[str] = None
    has_active_structure: bool = False
    annual_ctc: Optional[float] = None
    pt_state_code: Optional[str] = None
    tax_regime: Optional[str] = None
    has_primary_bank: bool = False
    bank_name: Optional[str] = None
    ifsc_code: Optional[str] = None
    kyc_warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Cycle Lifecycle, Entry Hold, Approval & Disbursement DTOs
# ---------------------------------------------------------------------------
class CycleCreateRequest(BaseModel):
    cycle_month: int = Field(..., ge=1, le=12)
    cycle_year: int = Field(..., ge=2020, le=2100)
    cycle_name: Optional[str] = Field(default=None, max_length=150)
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    cutoff_date: Optional[date] = None
    cycle_type: CycleType = "REGULAR"
    reference_cycle_id: Optional[str] = None

    @model_validator(mode="after")
    def _populate_and_validate_dates(self) -> CycleCreateRequest:
        last_day = calendar.monthrange(self.cycle_year, self.cycle_month)[1]
        if self.start_date is None:
            self.start_date = date(self.cycle_year, self.cycle_month, 1)
        if self.end_date is None:
            self.end_date = date(self.cycle_year, self.cycle_month, last_day)
        if self.cutoff_date is None:
            self.cutoff_date = self.end_date

        if not (self.start_date <= self.cutoff_date <= self.end_date):
            raise ValueError(
                "Dates must satisfy start_date <= cutoff_date <= end_date."
            )

        if not self.cycle_name or not self.cycle_name.strip():
            month_label = calendar.month_name[self.cycle_month]
            suffix = "" if self.cycle_type == "REGULAR" else f" ({self.cycle_type.replace('_', ' ').title()})"
            self.cycle_name = f"{month_label} {self.cycle_year} Payroll{suffix}"
        else:
            self.cycle_name = self.cycle_name.strip()

        if self.cycle_type == "REGULAR" and self.reference_cycle_id is not None:
            raise ValueError("REGULAR payroll cycles cannot specify reference_cycle_id.")

        return self


class CycleCalculateRequest(BaseModel):
    strict_attendance: bool = True
    proration_divisor: Decimal = Field(default=Decimal("30"), gt=Decimal("0"))
    as_of_date: Optional[date] = None
    preserve_holds: bool = True
    employee_ids: Optional[list[str]] = None


class EntryHoldRequest(BaseModel):
    reason: str = Field(..., min_length=3, max_length=500)

    @field_validator("reason")
    @classmethod
    def _val_reason(cls, v: str) -> str:
        val = v.strip()
        if len(val) < 3:
            raise ValueError("Hold reason must be at least 3 characters.")
        return val


class EntryReleaseRequest(BaseModel):
    note: Optional[str] = Field(default=None, max_length=500)


class CycleApprovalActionRequest(BaseModel):
    decision: ApprovalDecision
    comments: Optional[str] = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _val_rejection_comment(self) -> CycleApprovalActionRequest:
        if self.decision == "REJECTED" and (not self.comments or not self.comments.strip()):
            raise ValueError("comments are mandatory when rejecting a payroll cycle.")
        if self.comments is not None:
            self.comments = self.comments.strip() or None
        return self


class CycleMarkDisbursedRequest(BaseModel):
    bank_batch_reference: str = Field(..., min_length=3, max_length=120)
    utr_numbers: Optional[dict[str, str]] = None

    @field_validator("bank_batch_reference")
    @classmethod
    def _val_batch_ref(cls, v: str) -> str:
        val = v.strip()
        if len(val) < 3:
            raise ValueError("bank_batch_reference must be at least 3 characters.")
        return val


class PayrollCycleApprovalRow(BaseModel):
    id: str
    cycle_id: str
    step_order: int
    role_required: str
    approver_id: Optional[str] = None
    approver_name: Optional[str] = None
    status: str
    comments: Optional[str] = None
    decided_at: Optional[str] = None
    created_at: Optional[str] = None


class PayrollEntryRow(BaseModel):
    id: str
    cycle_id: str
    employee_id: str
    employee_name: Optional[str] = None
    employee_email: Optional[str] = None
    department: Optional[str] = None
    designation: Optional[str] = None
    pan_number: Optional[str] = None
    uan_number: Optional[str] = None
    status: EntryStatus
    hold_reason: Optional[str] = None
    total_calendar_days: int
    days_worked: float
    unpaid_leave_days: float
    unexcused_absent_days: float
    ncp_days: float
    earned_basic: float
    earned_hra: float
    earned_special_allowance: float
    earned_da: float
    gross_earnings: float
    epf_wages: float
    eps_wages: float
    esi_wages: float
    epf_employee_share: float
    eps_employer_share: float
    epf_employer_share: float
    edli_employer_share: float
    epf_admin_charges: float
    esi_employee_share: float
    esi_employer_share: float
    professional_tax: float
    tds_amount: float
    reimbursements_amount: float
    advance_recovery_amount: float
    net_payable: float
    gratuity_accrual_monthly: float
    total_employer_cost: float
    earnings_breakdown: dict[str, Any] = Field(default_factory=dict)
    deductions_breakdown: dict[str, Any] = Field(default_factory=dict)
    payslip_pdf_url: Optional[str] = None
    bank_utr_number: Optional[str] = None
    payout_status: PayoutStatus
    bank_verified: bool = False
    bank_response_code: Optional[str] = None
    bank_response_message: Optional[str] = None
    bank_transaction_date: Optional[str] = None
    reconciled_at: Optional[str] = None
    remediation_status: Optional[str] = "NONE"
    remediation_notes: Optional[str] = None
    paid_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class PayrollCycleSummary(BaseModel):
    id: str
    cycle_name: str
    cycle_month: int
    cycle_year: int
    start_date: str
    end_date: str
    cutoff_date: str
    cycle_type: CycleType
    reference_cycle_id: Optional[str] = None
    status: CycleStatus
    total_gross: float
    total_epf_employee: float
    total_eps_employer: float
    total_epf_employer: float
    total_esi_employee: float
    total_esi_employer: float
    total_pt: float
    total_tds: float
    total_reimbursements: float
    total_advance_deductions: float
    total_net_payable: float
    total_company_cost: float
    employee_count: int
    cleared_count: int = 0
    on_hold_count: int = 0
    bank_batch_reference: Optional[str] = None
    bank_export_url: Optional[str] = None
    ecr_export_url: Optional[str] = None
    created_by: Optional[str] = None
    approved_by: Optional[str] = None
    disbursed_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class PayrollCycleDetailResponse(BaseModel):
    cycle: PayrollCycleSummary
    entries: list[PayrollEntryRow] = Field(default_factory=list)
    approvals: list[PayrollCycleApprovalRow] = Field(default_factory=list)


class CycleActionStatusResponse(BaseModel):
    status: str
    message: str
    cycle: PayrollCycleSummary


# ---------------------------------------------------------------------------
# Employee Self-Service DTOs
# ---------------------------------------------------------------------------
class EmployeePayslipListItem(BaseModel):
    entry_id: str
    cycle_id: str
    cycle_name: str
    cycle_month: int
    cycle_year: int
    cycle_type: CycleType
    cycle_status: CycleStatus
    payout_status: PayoutStatus
    days_worked: float
    ncp_days: float
    gross_earnings: float
    total_deductions: float
    reimbursements_amount: float
    net_payable: float
    paid_at: Optional[str] = None
    bank_utr_number: Optional[str] = None
    payslip_pdf_url: Optional[str] = None


class EmployeePayslipDetailResponse(BaseModel):
    entry: PayrollEntryRow
    cycle: PayrollCycleSummary
    company: Optional[CompanyStatutoryResponse] = None
    bank_details: Optional[BankDetailsResponse] = None


class ProvisionalPreviewResponse(BaseModel):
    has_salary_structure: bool
    message: Optional[str] = None
    cycle_month: int
    cycle_year: int
    as_of_date: str
    attendance_summary: Optional[dict[str, Any]] = None
    leave_summary: Optional[dict[str, Any]] = None
    calculation: Optional[dict[str, Any]] = None
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Phase 6 & 7: End-to-End Banking, Reconciliation, Vault & Statutory DTOs
# ---------------------------------------------------------------------------
DocumentCategory = Literal["BANKING", "EPFO", "ESIC", "PROFESSIONAL_TAX", "TDS", "GENERAL_PAYROLL"]
PaymentBatchStatus = Literal["GENERATED", "SUBMITTED_TO_BANK", "BANK_ACCEPTED", "PROCESSING", "RECONCILED", "PARTIALLY_SETTLED", "FAILED"]
MatchOutcome = Literal[
    "EXACT_MATCH",
    "PARTIAL_MATCH",
    "AMOUNT_MISMATCH",
    "ACCOUNT_MISMATCH",
    "DUPLICATE_BANK_RECORD",
    "UNMATCHED",
    "FAILED_TRANSACTION",
    "REVERSED_TRANSACTION",
    "REQUIRES_REVIEW",
]
StatutoryCategory = Literal["EPFO", "ESIC", "PT", "TDS"]
StatutoryRemittanceStatus = Literal[
    "NOT_GENERATED",
    "GENERATED",
    "SUBMITTED",
    "ACKNOWLEDGED",
    "CHALLAN_GENERATED",
    "PAYMENT_PENDING",
    "PARTIALLY_PAID",
    "PAID",
    "RECONCILED",
    "REQUIRES_REVIEW",
    "FAILED",
]


class PayrollDocumentMetadata(BaseModel):
    id: str
    organization_id: Optional[str] = None
    cycle_id: Optional[str] = None
    employee_id: Optional[str] = None
    document_category: DocumentCategory
    document_type: str
    financial_year: Optional[str] = None
    wage_month: Optional[int] = None
    wage_year: Optional[int] = None
    original_filename: str
    file_size_bytes: int
    mime_type: str
    sha256_checksum: str
    external_reference_number: Optional[str] = None
    notes: Optional[str] = None
    is_archived: bool = False
    uploaded_by: Optional[str] = None
    created_at: str


class BankBatchSubmissionRequest(BaseModel):
    batch_reference: str
    bank_name: str
    submission_timestamp: Optional[str] = None
    ack_reference: Optional[str] = None
    notes: Optional[str] = None


class BankBatchResponse(BaseModel):
    id: str
    cycle_id: str
    batch_reference: str
    bank_name: str
    payment_mode: str
    total_beneficiaries: int
    total_amount: float
    status: PaymentBatchStatus
    export_filename: Optional[str] = None
    file_checksum_sha256: Optional[str] = None
    submission_timestamp: Optional[str] = None
    ack_reference: Optional[str] = None
    ack_timestamp: Optional[str] = None
    created_at: str
    updated_at: str


class PaymentTransactionRow(BaseModel):
    id: str
    reconciliation_id: str
    cycle_id: str
    payroll_entry_id: Optional[str] = None
    employee_id: Optional[str] = None
    employee_name: Optional[str] = None
    account_number_mask: Optional[str] = None
    ifsc_code: Optional[str] = None
    amount: float
    bank_utr: Optional[str] = None
    bank_status: str
    bank_response_code: Optional[str] = None
    bank_response_message: Optional[str] = None
    match_outcome: MatchOutcome
    is_settled: bool
    created_at: str


class BankReconciliationSummaryResponse(BaseModel):
    reconciliation_id: Optional[str] = None
    cycle_id: str
    total_payment_instructions: int
    total_amount_submitted: float
    successful_count: int
    successful_amount: float
    failed_count: int
    failed_amount: float
    pending_count: int
    pending_amount: float
    reversed_count: int
    reversed_amount: float
    review_count: int
    unmatched_count: int
    transactions: list[PaymentTransactionRow] = Field(default_factory=list)


class RemediateFailureRequest(BaseModel):
    remediation_status: Literal["NEEDS_CORRECTION", "DETAILS_UPDATED", "OFF_CYCLE_QUEUED", "RESOLVED"]
    notes: Optional[str] = None
    corrected_account_number: Optional[str] = None
    corrected_ifsc_code: Optional[str] = None


class OffCycleInitiateRequest(BaseModel):
    target_entry_ids: list[str]
    notes: Optional[str] = None


class StatutoryRemittanceRow(BaseModel):
    id: str
    cycle_id: str
    category: StatutoryCategory
    financial_year: str
    wage_month: int
    wage_year: int
    calculated_liability: float
    declared_liability: float
    challan_amount: float
    paid_amount: float
    acknowledged_amount: float
    difference_amount: float = 0.0
    status: StatutoryRemittanceStatus
    due_date: str
    payment_date: Optional[str] = None
    filing_date: Optional[str] = None
    external_reference: Optional[str] = None
    token_number: Optional[str] = None
    notes: Optional[str] = None
    challan_document_id: Optional[str] = None
    receipt_document_id: Optional[str] = None
    reconciled_at: Optional[str] = None
    updated_at: str


class StatutoryRemittanceUpdateRequest(BaseModel):
    declared_liability: Optional[float] = None
    challan_amount: Optional[float] = None
    paid_amount: Optional[float] = None
    acknowledged_amount: Optional[float] = None
    status: Optional[StatutoryRemittanceStatus] = None
    payment_date: Optional[str] = None
    filing_date: Optional[str] = None
    external_reference: Optional[str] = None
    token_number: Optional[str] = None
    notes: Optional[str] = None


class PayrollAuditLogRow(BaseModel):
    id: str
    cycle_id: Optional[str] = None
    entry_id: Optional[str] = None
    actor_id: Optional[str] = None
    actor_role: Optional[str] = None
    action: str
    entity_type: str
    entity_id: str
    previous_state: Optional[dict[str, Any]] = None
    new_state: Optional[dict[str, Any]] = None
    comments: Optional[str] = None
    created_at: str

