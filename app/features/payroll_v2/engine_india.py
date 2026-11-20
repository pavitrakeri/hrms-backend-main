"""
Pure Python Indian Statutory Calculation Engine (Payroll V2).

Zero database queries, zero network I/O, 100% deterministic Decimal arithmetic.
Governing statutes & primary reference: payroll_technical_design_document.md (v2.0.0)
and migrations/015_india_payroll_schema.sql.

Statutes implemented:
  1. Employees' Provident Funds & Miscellaneous Provisions Act, 1952 (EPF / EPS / EDLI)
  2. Employees' State Insurance Act, 1948 (ESIC 0.75% / 3.25%, Rs. 21,000 ceiling)
  3. State Professional Tax Acts (KA, MH incl. Feb Rs. 300 bump, TS, DL, TN, or DB slabs)
  4. Payment of Gratuity Act, 1972 (15/26 monthly accrual on Basic + DA)
  5. Income Tax Act, 1961 Section 192 (New Regime Sec 115BAC & Old Regime + Cess 4%)
"""

from __future__ import annotations

import calendar
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Optional, Sequence

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Statutory Constants (All monetary values in INR as Decimal)
# ---------------------------------------------------------------------------
ZERO = Decimal("0")
ZERO_PAISA = Decimal("0.00")
ONE_RUPEE = Decimal("1")
ONE_PAISA = Decimal("0.01")
HUNDRED = Decimal("100")

DEFAULT_PRORATION_DIVISOR = Decimal("30")

EPF_STATUTORY_WAGE_CEILING = Decimal("15000")
EPS_STATUTORY_WAGE_CEILING = Decimal("15000")
EDLI_STATUTORY_WAGE_CEILING = Decimal("15000")

EPF_RATE = Decimal("0.12")
EPS_RATE = Decimal("0.0833")
EPS_MAX_MONTHLY_CONTRIBUTION = Decimal("1250")
EDLI_RATE = Decimal("0.005")
EDLI_MAX_MONTHLY_CONTRIBUTION = Decimal("75")
EPF_ADMIN_RATE = Decimal("0.005")

ESI_GROSS_CEILING = Decimal("21000")
ESI_EMPLOYEE_RATE = Decimal("0.0075")
ESI_EMPLOYER_RATE = Decimal("0.0325")

GRATUITY_DAYS_NUMERATOR = Decimal("15")
GRATUITY_DAYS_DENOMINATOR = Decimal("26")
MONTHS_IN_YEAR = Decimal("12")

STD_DEDUCTION_NEW_REGIME = Decimal("75000")
STD_DEDUCTION_OLD_REGIME = Decimal("50000")
REBATE_87A_LIMIT_NEW = Decimal("700000")
REBATE_87A_LIMIT_OLD = Decimal("500000")
REBATE_87A_MAX_OLD = Decimal("12500")
HEALTH_EDU_CESS_RATE = Decimal("0.04")

CAP_80C = Decimal("150000")
CAP_80CCD_1B = Decimal("50000")
CAP_80D = Decimal("100000")
CAP_SEC_24B = Decimal("200000")
MAX_ANNUAL_PT_DEDUCTION = Decimal("2500")


def round_rupee(value: Decimal) -> Decimal:
    """Round monetary amount to nearest integer INR (ROUND_HALF_UP)."""
    return value.quantize(ONE_RUPEE, rounding=ROUND_HALF_UP)


def round_paisa(value: Decimal) -> Decimal:
    """Round monetary amount to 2 decimal places / paise (ROUND_HALF_UP)."""
    return value.quantize(ONE_PAISA, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Input & Reference Models
# ---------------------------------------------------------------------------
class PTSlab(BaseModel):
    """Mirrors `pt_state_slabs` table schema in 015_india_payroll_schema.sql."""

    state_code: str
    state_name: str = ""
    min_monthly_gross: Decimal = ZERO
    max_monthly_gross: Optional[Decimal] = None
    pt_amount: Decimal = ZERO
    special_feb_amount: Optional[Decimal] = None


# Built-in fallback state slabs synchronized with migrations/015_india_payroll_schema.sql
DEFAULT_PT_SLABS: tuple[PTSlab, ...] = (
    # Karnataka (KA)
    PTSlab(state_code="KA", state_name="Karnataka", min_monthly_gross=Decimal("0"), max_monthly_gross=Decimal("15000"), pt_amount=Decimal("0")),
    PTSlab(state_code="KA", state_name="Karnataka", min_monthly_gross=Decimal("15000.01"), max_monthly_gross=None, pt_amount=Decimal("200")),
    # Maharashtra (MH): Rs. 200 for 11 months, Rs. 300 in February
    PTSlab(state_code="MH", state_name="Maharashtra", min_monthly_gross=Decimal("0"), max_monthly_gross=Decimal("7500"), pt_amount=Decimal("0")),
    PTSlab(state_code="MH", state_name="Maharashtra", min_monthly_gross=Decimal("7500.01"), max_monthly_gross=Decimal("10000"), pt_amount=Decimal("175")),
    PTSlab(state_code="MH", state_name="Maharashtra", min_monthly_gross=Decimal("10000.01"), max_monthly_gross=None, pt_amount=Decimal("200"), special_feb_amount=Decimal("300")),
    # Telangana (TS)
    PTSlab(state_code="TS", state_name="Telangana", min_monthly_gross=Decimal("0"), max_monthly_gross=Decimal("15000"), pt_amount=Decimal("0")),
    PTSlab(state_code="TS", state_name="Telangana", min_monthly_gross=Decimal("15000.01"), max_monthly_gross=Decimal("20000"), pt_amount=Decimal("150")),
    PTSlab(state_code="TS", state_name="Telangana", min_monthly_gross=Decimal("20000.01"), max_monthly_gross=None, pt_amount=Decimal("200")),
    # Delhi (DL): No Professional Tax
    PTSlab(state_code="DL", state_name="Delhi", min_monthly_gross=Decimal("0"), max_monthly_gross=None, pt_amount=Decimal("0")),
    # Tamil Nadu (TN): Monthly equivalent from half-yearly Chennai/TN slabs
    PTSlab(state_code="TN", state_name="Tamil Nadu", min_monthly_gross=Decimal("0"), max_monthly_gross=Decimal("3500"), pt_amount=Decimal("0")),
    PTSlab(state_code="TN", state_name="Tamil Nadu", min_monthly_gross=Decimal("3500.01"), max_monthly_gross=Decimal("5000"), pt_amount=Decimal("23")),
    PTSlab(state_code="TN", state_name="Tamil Nadu", min_monthly_gross=Decimal("5000.01"), max_monthly_gross=Decimal("7500"), pt_amount=Decimal("53")),
    PTSlab(state_code="TN", state_name="Tamil Nadu", min_monthly_gross=Decimal("7500.01"), max_monthly_gross=Decimal("10000"), pt_amount=Decimal("115")),
    PTSlab(state_code="TN", state_name="Tamil Nadu", min_monthly_gross=Decimal("10000.01"), max_monthly_gross=Decimal("12500"), pt_amount=Decimal("171")),
    PTSlab(state_code="TN", state_name="Tamil Nadu", min_monthly_gross=Decimal("12500.01"), max_monthly_gross=None, pt_amount=Decimal("208")),
)


class TaxDeclarationInput(BaseModel):
    """Mirrors `employee_tax_declarations` in 015_india_payroll_schema.sql."""

    regime: Optional[Literal["NEW", "OLD"]] = None
    section_80c_amount: Decimal = Field(default=ZERO, ge=ZERO)
    section_80d_amount: Decimal = Field(default=ZERO, ge=ZERO)
    annual_rent_paid: Decimal = Field(default=ZERO, ge=ZERO)
    home_loan_interest: Decimal = Field(default=ZERO, ge=ZERO)
    nps_80ccd_1b: Decimal = Field(default=ZERO, ge=ZERO)
    is_metro: bool = True
    include_epf_in_80c: bool = False


class PayrollEmployeeInput(BaseModel):
    """
    Complete typed input bundle representing one employee for one monthly cycle.
    Maps directly to `employee_salary_structures`, `employee_tax_declarations`,
    and aggregated outputs from attendance/leave/expense/advance adapters.
    """

    employee_id: str = ""
    employee_name: str = ""
    cycle_month: int = Field(ge=1, le=12)
    cycle_year: int = Field(ge=2020)
    total_calendar_days: Optional[int] = Field(default=None, ge=28, le=31)
    proration_divisor: Decimal = Field(default=DEFAULT_PRORATION_DIVISOR, gt=ZERO)

    # Contractual monthly components (`employee_salary_structures`)
    monthly_basic_pay: Decimal = Field(ge=ZERO)
    monthly_hra: Decimal = Field(default=ZERO, ge=ZERO)
    monthly_special_allowance: Decimal = Field(default=ZERO, ge=ZERO)
    monthly_da: Decimal = Field(default=ZERO, ge=ZERO)

    # Statutory compliance switches
    is_epf_eligible: bool = True
    epf_wage_cap_applied: bool = True
    is_eps_eligible: bool = True
    vpf_percentage: Decimal = Field(default=ZERO, ge=ZERO, le=Decimal("100"))
    is_esi_eligible: Optional[bool] = None
    pt_state_code: str = "KA"
    tax_regime: Literal["NEW", "OLD"] = "NEW"

    # Monthly attendance & LOP (`payroll_entries` day counts)
    unpaid_leave_days: Decimal = Field(default=ZERO, ge=ZERO)
    unexcused_absent_days: Decimal = Field(default=ZERO, ge=ZERO)

    # Adjustments
    approved_reimbursements: Decimal = Field(default=ZERO, ge=ZERO)
    scheduled_advance_emi: Decimal = Field(default=ZERO, ge=ZERO)

    # Optional tax declaration & PT slabs override
    tax_declaration: Optional[TaxDeclarationInput] = None
    pt_slabs: Optional[list[PTSlab]] = None

    # Optional Financial Year YTD tracking (enables end-of-year true-up)
    ytd_gross_earned: Optional[Decimal] = Field(default=None, ge=ZERO)
    ytd_basic_plus_da: Optional[Decimal] = Field(default=None, ge=ZERO)
    ytd_hra_received: Optional[Decimal] = Field(default=None, ge=ZERO)
    ytd_pt_paid: Optional[Decimal] = Field(default=None, ge=ZERO)
    ytd_epf_employee: Optional[Decimal] = Field(default=None, ge=ZERO)
    ytd_tds_deducted: Decimal = Field(default=ZERO, ge=ZERO)

    @model_validator(mode="after")
    def _validate_calendar_and_ncp(self) -> PayrollEmployeeInput:
        actual_cal_days = calendar.monthrange(self.cycle_year, self.cycle_month)[1]
        if self.total_calendar_days is None:
            self.total_calendar_days = actual_cal_days
        total_ncp = self.unpaid_leave_days + self.unexcused_absent_days
        if total_ncp > Decimal(self.total_calendar_days):
            raise ValueError(
                f"Total NCP days ({total_ncp}) cannot exceed total calendar days ({self.total_calendar_days})."
            )
        return self

    @property
    def contractual_monthly_gross(self) -> Decimal:
        return round_paisa(
            self.monthly_basic_pay
            + self.monthly_hra
            + self.monthly_special_allowance
            + self.monthly_da
        )


# ---------------------------------------------------------------------------
# Sub-Calculation Output Models
# ---------------------------------------------------------------------------
class ProrationResult(BaseModel):
    total_calendar_days: int
    proration_divisor: Decimal
    days_worked: Decimal
    unpaid_leave_days: Decimal
    unexcused_absent_days: Decimal
    ncp_days: Decimal
    payable_ratio: Decimal
    earned_basic: Decimal
    earned_hra: Decimal
    earned_special_allowance: Decimal
    earned_da: Decimal
    gross_earnings: Decimal
    contractual_monthly_gross: Decimal
    lop_deduction_informational: Decimal


class EPFEPSResult(BaseModel):
    is_epf_eligible: bool
    is_eps_eligible: bool
    epf_wages: Decimal
    eps_wages: Decimal
    edli_wages: Decimal
    epf_employee_share: Decimal
    vpf_employee_share: Decimal
    eps_employer_share: Decimal
    epf_employer_share: Decimal
    edli_employer_share: Decimal
    epf_admin_charges: Decimal


class ESIResult(BaseModel):
    is_esi_eligible: bool
    esi_wages: Decimal
    esi_employee_share: Decimal
    esi_employer_share: Decimal


class TaxComputationBreakdown(BaseModel):
    regime: Literal["NEW", "OLD"]
    projected_annual_gross: Decimal
    standard_deduction: Decimal
    professional_tax_exemption: Decimal
    hra_exemption: Decimal
    section_80c_allowed: Decimal
    section_80d_allowed: Decimal
    section_80ccd_1b_allowed: Decimal
    section_24b_allowed: Decimal
    total_exemptions_and_deductions: Decimal
    taxable_income: Decimal
    slab_tax: Decimal
    rebate_87a: Decimal
    tax_after_rebate: Decimal
    surcharge: Decimal
    health_and_education_cess: Decimal
    annual_tax_liability: Decimal
    remaining_months_in_fy: int
    monthly_tds_computed: Decimal


class PayrollCalculationResult(BaseModel):
    """
    Complete deterministic payroll output for one employee in one cycle.
    Field names align 1-to-1 with columns in `payroll_entries`
    (migrations/015_india_payroll_schema.sql).
    """

    employee_id: str
    employee_name: str
    cycle_month: int
    cycle_year: int

    # Attendance & Non-Contributing Period
    total_calendar_days: int
    days_worked: Decimal
    unpaid_leave_days: Decimal
    unexcused_absent_days: Decimal
    ncp_days: Decimal

    # Earned Salary Components
    earned_basic: Decimal
    earned_hra: Decimal
    earned_special_allowance: Decimal
    earned_da: Decimal
    gross_earnings: Decimal

    # Statutory Wage Bases
    epf_wages: Decimal
    eps_wages: Decimal
    edli_wages: Decimal
    esi_wages: Decimal

    # Statutory Deductions & Employer Contributions
    epf_employee_share: Decimal
    vpf_employee_share: Decimal
    eps_employer_share: Decimal
    epf_employer_share: Decimal
    edli_employer_share: Decimal
    epf_admin_charges: Decimal
    esi_employee_share: Decimal
    esi_employer_share: Decimal
    professional_tax: Decimal
    tds_amount: Decimal

    # Payout Adjustments & Totals
    reimbursements_amount: Decimal
    advance_recovery_amount: Decimal
    total_employee_deductions: Decimal
    net_payable: Decimal
    gratuity_accrual_monthly: Decimal
    total_employer_cost: Decimal

    # Informational & Audit Breakdowns (JSONB ready)
    lop_deduction_informational: Decimal
    earnings_breakdown: dict[str, Any]
    deductions_breakdown: dict[str, Any]
    tax_breakdown: TaxComputationBreakdown


# ---------------------------------------------------------------------------
# Pure Pillar Calculation Functions
# ---------------------------------------------------------------------------
def calculate_proration(
    *,
    monthly_basic_pay: Decimal,
    monthly_hra: Decimal,
    monthly_special_allowance: Decimal,
    monthly_da: Decimal,
    total_calendar_days: int,
    unpaid_leave_days: Decimal = ZERO,
    unexcused_absent_days: Decimal = ZERO,
    proration_divisor: Decimal = DEFAULT_PRORATION_DIVISOR,
) -> ProrationResult:
    """
    Prorate fixed monthly components up-front without double-deducting LOP (fixes DEF-01).

    Boundary handling across 28/29/30/31-day months with default 30-day statutory divisor:
      - 0 NCP days                       -> 100% of contractual salary
      - days_worked <= 0 (full LOP)      -> 0% (Rs. 0.00)
      - ncp_days >= divisor & worked > 0 -> days_worked / divisor (prevents Rs. 0 when working 1 day in a 31-day month)
      - Standard partial absence         -> (divisor - ncp_days) / divisor
    """
    cal_days_dec = Decimal(total_calendar_days)
    ncp_days = unpaid_leave_days + unexcused_absent_days
    if ncp_days > cal_days_dec:
        raise ValueError(
            f"ncp_days ({ncp_days}) cannot exceed total_calendar_days ({total_calendar_days})."
        )
    days_worked = cal_days_dec - ncp_days

    if ncp_days == ZERO:
        ratio = Decimal("1")
    elif days_worked <= ZERO:
        ratio = ZERO
    elif ncp_days >= proration_divisor or (
        days_worked < ncp_days and (proration_divisor - ncp_days) > days_worked
    ):
        ratio = days_worked / proration_divisor
    else:
        ratio = (proration_divisor - ncp_days) / proration_divisor

    earned_basic = round_paisa(monthly_basic_pay * ratio)
    earned_hra = round_paisa(monthly_hra * ratio)
    earned_special = round_paisa(monthly_special_allowance * ratio)
    earned_da = round_paisa(monthly_da * ratio)
    gross_earnings = round_paisa(earned_basic + earned_hra + earned_special + earned_da)

    contractual_gross = round_paisa(
        monthly_basic_pay + monthly_hra + monthly_special_allowance + monthly_da
    )
    lop_informational = max(ZERO_PAISA, round_paisa(contractual_gross - gross_earnings))

    return ProrationResult(
        total_calendar_days=total_calendar_days,
        proration_divisor=proration_divisor,
        days_worked=days_worked,
        unpaid_leave_days=unpaid_leave_days,
        unexcused_absent_days=unexcused_absent_days,
        ncp_days=ncp_days,
        payable_ratio=ratio,
        earned_basic=earned_basic,
        earned_hra=earned_hra,
        earned_special_allowance=earned_special,
        earned_da=earned_da,
        gross_earnings=gross_earnings,
        contractual_monthly_gross=contractual_gross,
        lop_deduction_informational=lop_informational,
    )


def calculate_epf_and_eps(
    *,
    earned_basic: Decimal,
    earned_da: Decimal = ZERO,
    is_epf_eligible: bool = True,
    epf_wage_cap_applied: bool = True,
    is_eps_eligible: bool = True,
    vpf_percentage: Decimal = ZERO,
) -> EPFEPSResult:
    """
    Compute EPF, EPS (capped at Rs. 1,250), EDLI (0.5% capped at Rs. 75),
    EPF Admin Charges (0.5%), and optional Voluntary PF (VPF).
    Guarantees EPFO ECR checksum invariant: ee_epf == er_eps + er_epf.
    """
    actual_epf_base = round_paisa(earned_basic + earned_da)
    if not is_epf_eligible or actual_epf_base <= ZERO:
        return EPFEPSResult(
            is_epf_eligible=False,
            is_eps_eligible=False,
            epf_wages=ZERO_PAISA,
            eps_wages=ZERO_PAISA,
            edli_wages=ZERO_PAISA,
            epf_employee_share=ZERO,
            vpf_employee_share=ZERO,
            eps_employer_share=ZERO,
            epf_employer_share=ZERO,
            edli_employer_share=ZERO,
            epf_admin_charges=ZERO,
        )

    capped_base = min(actual_epf_base, EPF_STATUTORY_WAGE_CEILING)
    epf_wages_raw = capped_base if epf_wage_cap_applied else actual_epf_base
    epf_wages = round_rupee(epf_wages_raw)

    eps_wages = min(epf_wages, EPS_STATUTORY_WAGE_CEILING) if is_eps_eligible else ZERO
    edli_wages = min(epf_wages, EDLI_STATUTORY_WAGE_CEILING)

    epf_employee_share = round_rupee(epf_wages * EPF_RATE)
    vpf_employee_share = (
        round_rupee(actual_epf_base * (vpf_percentage / HUNDRED))
        if vpf_percentage > ZERO
        else ZERO
    )

    if is_eps_eligible and eps_wages > ZERO:
        eps_employer_share = min(
            round_rupee(eps_wages * EPS_RATE), EPS_MAX_MONTHLY_CONTRIBUTION
        )
    else:
        eps_employer_share = ZERO

    # Maintains exact EPFO ECR balance: EE Share == EPS Share + ER Share
    epf_employer_share = max(ZERO, epf_employee_share - eps_employer_share)

    edli_employer_share = min(
        round_rupee(edli_wages * EDLI_RATE), EDLI_MAX_MONTHLY_CONTRIBUTION
    )
    epf_admin_charges = round_rupee(epf_wages * EPF_ADMIN_RATE)

    return EPFEPSResult(
        is_epf_eligible=True,
        is_eps_eligible=is_eps_eligible,
        epf_wages=epf_wages,
        eps_wages=eps_wages,
        edli_wages=edli_wages,
        epf_employee_share=epf_employee_share,
        vpf_employee_share=vpf_employee_share,
        eps_employer_share=eps_employer_share,
        epf_employer_share=epf_employer_share,
        edli_employer_share=edli_employer_share,
        epf_admin_charges=epf_admin_charges,
    )


def calculate_esi(
    *,
    earned_gross: Decimal,
    contractual_monthly_gross: Optional[Decimal] = None,
    is_esi_eligible: Optional[bool] = None,
    is_company_esi_covered: bool = True,
) -> ESIResult:
    """
    Compute Employees' State Insurance (0.75% EE / 3.25% ER).
    Applies the two-tier statutory test:
      1. Tier 1 (Establishment): `is_company_esi_covered` (ESI Act 10+ workers gate).
      2. Tier 2 (Individual): contractual monthly gross <= Rs. 21,000 (ESI_GROSS_CEILING).
    """
    if not is_company_esi_covered:
        return ESIResult(
            is_esi_eligible=False,
            esi_wages=ZERO_PAISA,
            esi_employee_share=ZERO,
            esi_employer_share=ZERO,
        )

    threshold_basis = (
        contractual_monthly_gross if contractual_monthly_gross is not None else earned_gross
    )
    eligible = (
        threshold_basis <= ESI_GROSS_CEILING
        if is_esi_eligible is None
        else bool(is_esi_eligible)
    )

    if not eligible or earned_gross <= ZERO:
        return ESIResult(
            is_esi_eligible=eligible,
            esi_wages=ZERO_PAISA,
            esi_employee_share=ZERO,
            esi_employer_share=ZERO,
        )

    esi_wages = round_paisa(earned_gross)
    esi_ee = round_rupee(esi_wages * ESI_EMPLOYEE_RATE)
    esi_er = round_rupee(esi_wages * ESI_EMPLOYER_RATE)

    return ESIResult(
        is_esi_eligible=True,
        esi_wages=esi_wages,
        esi_employee_share=esi_ee,
        esi_employer_share=esi_er,
    )


def calculate_professional_tax(
    *,
    earned_gross: Decimal,
    pt_state_code: str,
    cycle_month: int,
    slabs: Optional[Sequence[PTSlab]] = None,
) -> Decimal:
    """
    Evaluate State Professional Tax strictly against `pt_state_slabs` rules.
    Handles Maharashtra February special levy (Rs. 300 in month == 2).
    """
    if earned_gross <= ZERO:
        return ZERO

    state = pt_state_code.strip().upper()
    active_slabs = slabs if slabs is not None and len(slabs) > 0 else DEFAULT_PT_SLABS
    state_slabs = [s for s in active_slabs if s.state_code.strip().upper() == state]
    if not state_slabs:
        return ZERO

    gross_2dp = round_paisa(earned_gross)
    state_slabs.sort(key=lambda s: s.min_monthly_gross)

    for slab in state_slabs:
        above_min = gross_2dp >= slab.min_monthly_gross
        below_max = (
            slab.max_monthly_gross is None or gross_2dp <= slab.max_monthly_gross
        )
        if above_min and below_max:
            if cycle_month == 2 and slab.special_feb_amount is not None:
                return round_rupee(slab.special_feb_amount)
            return round_rupee(slab.pt_amount)

    return ZERO


def calculate_gratuity_accrual(
    *,
    earned_basic: Decimal,
    earned_da: Decimal = ZERO,
) -> Decimal:
    """
    Payment of Gratuity Act, 1972 employer balance-sheet accrual:
    Monthly Gratuity = (Basic + DA) * (15 / 26) * (1 / 12) (~4.81%).
    """
    wage_base = earned_basic + earned_da
    if wage_base <= ZERO:
        return ZERO_PAISA
    accrual = (
        wage_base
        * GRATUITY_DAYS_NUMERATOR
        / GRATUITY_DAYS_DENOMINATOR
        / MONTHS_IN_YEAR
    )
    return round_paisa(accrual)


def calculate_hra_exemption(
    *,
    annual_basic_plus_da: Decimal,
    annual_hra_received: Decimal,
    annual_rent_paid: Decimal,
    is_metro: bool = True,
) -> Decimal:
    """
    Income Tax Act Section 10(13A) HRA exemption (Old Tax Regime only).
    Minimum of:
      1. Actual HRA received
      2. Rent paid minus 10% of (Basic + DA)
      3. 50% of (Basic + DA) for metro cities, else 40%
    """
    if annual_rent_paid <= ZERO or annual_hra_received <= ZERO or annual_basic_plus_da <= ZERO:
        return ZERO
    excess_rent = max(ZERO, annual_rent_paid - (annual_basic_plus_da * Decimal("0.10")))
    metro_cap = annual_basic_plus_da * (
        Decimal("0.50") if is_metro else Decimal("0.40")
    )
    return round_rupee(min(annual_hra_received, excess_rent, metro_cap))


def _compute_progressive_slabs(
    taxable_income: Decimal,
    slabs: Sequence[tuple[Optional[Decimal], Decimal]],
) -> Decimal:
    """Compute tax across progressive `(upper_bound, rate)` brackets."""
    tax = ZERO
    prev_limit = ZERO
    for upper, rate in slabs:
        if taxable_income <= prev_limit:
            break
        slice_income = (
            min(taxable_income, upper) - prev_limit
            if upper is not None
            else taxable_income - prev_limit
        )
        if slice_income > ZERO:
            tax += slice_income * rate
        if upper is None:
            break
        prev_limit = upper
    return round_paisa(tax)


def _compute_surcharge(
    base_tax: Decimal,
    taxable_income: Decimal,
    regime: Literal["NEW", "OLD"],
) -> Decimal:
    """Compute statutory Income Tax surcharge with marginal relief."""
    if base_tax <= ZERO or taxable_income <= Decimal("5000000"):
        return ZERO

    if taxable_income <= Decimal("10000000"):
        rate, threshold = Decimal("0.10"), Decimal("5000000")
        prev_rate = ZERO
    elif taxable_income <= Decimal("20000000"):
        rate, threshold = Decimal("0.15"), Decimal("10000000")
        prev_rate = Decimal("0.10")
    elif regime == "NEW" or taxable_income <= Decimal("50000000"):
        rate, threshold = Decimal("0.25"), Decimal("20000000")
        prev_rate = Decimal("0.15")
    else:
        rate, threshold = Decimal("0.37"), Decimal("50000000")
        prev_rate = Decimal("0.25")

    surcharge = base_tax * rate
    # Statutory marginal relief against surcharge jump at threshold
    tax_at_threshold = _compute_progressive_slabs(
        threshold,
        _NEW_REGIME_SLABS if regime == "NEW" else _OLD_REGIME_SLABS,
    ) * (Decimal("1") + prev_rate)
    max_total_tax = tax_at_threshold + (taxable_income - threshold)
    total_with_surcharge = base_tax + surcharge
    if total_with_surcharge > max_total_tax:
        surcharge = max(ZERO, max_total_tax - base_tax)

    return round_paisa(surcharge)


_NEW_REGIME_SLABS: tuple[tuple[Optional[Decimal], Decimal], ...] = (
    (Decimal("300000"), Decimal("0.00")),
    (Decimal("700000"), Decimal("0.05")),
    (Decimal("1000000"), Decimal("0.10")),
    (Decimal("1200000"), Decimal("0.15")),
    (Decimal("1500000"), Decimal("0.20")),
    (None, Decimal("0.30")),
)

_OLD_REGIME_SLABS: tuple[tuple[Optional[Decimal], Decimal], ...] = (
    (Decimal("250000"), Decimal("0.00")),
    (Decimal("500000"), Decimal("0.05")),
    (Decimal("1000000"), Decimal("0.20")),
    (None, Decimal("0.30")),
)


def calculate_annual_income_tax(
    *,
    projected_annual_gross: Decimal,
    regime: Literal["NEW", "OLD"] = "NEW",
    annual_basic_plus_da: Decimal = ZERO,
    annual_hra_received: Decimal = ZERO,
    annual_pt_paid: Decimal = ZERO,
    annual_epf_employee: Decimal = ZERO,
    tax_declaration: Optional[TaxDeclarationInput] = None,
    remaining_months_in_fy: int = 12,
    monthly_tds_computed: Decimal = ZERO,
) -> TaxComputationBreakdown:
    """
    Compute full annual tax liability under Income Tax Act, 1961 Section 192.
    Supports:
      - New Tax Regime (Sec 115BAC): Rs. 75k std deduction, Sec 87A rebate + marginal relief
      - Old Tax Regime: Rs. 50k std deduction, PT u/s 16(iii), HRA u/s 10(13A),
        80C (cap 1.5L), 80D, 80CCD(1B) (cap 50k), Sec 24(b) (cap 2L), Sec 87A up to Rs. 5L
      - Surcharge + 4% Health & Education Cess
    """
    decl = tax_declaration or TaxDeclarationInput()
    effective_regime: Literal["NEW", "OLD"] = decl.regime or regime

    if effective_regime == "NEW":
        std_ded = min(projected_annual_gross, STD_DEDUCTION_NEW_REGIME)
        pt_exemption = ZERO
        hra_exemption = ZERO
        sec_80c = ZERO
        sec_80d = ZERO
        sec_80ccd_1b = ZERO
        sec_24b = ZERO
    else:
        std_ded = min(projected_annual_gross, STD_DEDUCTION_OLD_REGIME)
        pt_exemption = min(max(ZERO, annual_pt_paid), MAX_ANNUAL_PT_DEDUCTION)
        hra_exemption = calculate_hra_exemption(
            annual_basic_plus_da=annual_basic_plus_da,
            annual_hra_received=annual_hra_received,
            annual_rent_paid=decl.annual_rent_paid,
            is_metro=decl.is_metro,
        )
        raw_80c = decl.section_80c_amount + (
            annual_epf_employee if decl.include_epf_in_80c else ZERO
        )
        sec_80c = min(raw_80c, CAP_80C)
        sec_80d = min(decl.section_80d_amount, CAP_80D)
        sec_80ccd_1b = min(decl.nps_80ccd_1b, CAP_80CCD_1B)
        sec_24b = min(decl.home_loan_interest, CAP_SEC_24B)

    total_exemptions = round_paisa(
        std_ded
        + pt_exemption
        + hra_exemption
        + sec_80c
        + sec_80d
        + sec_80ccd_1b
        + sec_24b
    )
    taxable_income = max(ZERO, round_paisa(projected_annual_gross - total_exemptions))

    slabs = _NEW_REGIME_SLABS if effective_regime == "NEW" else _OLD_REGIME_SLABS
    slab_tax = _compute_progressive_slabs(taxable_income, slabs)

    # Section 87A Rebate (and marginal relief in New Regime)
    rebate = ZERO
    if effective_regime == "NEW":
        if taxable_income <= REBATE_87A_LIMIT_NEW:
            rebate = slab_tax
        else:
            excess_income = taxable_income - REBATE_87A_LIMIT_NEW
            if slab_tax > excess_income:
                rebate = round_paisa(slab_tax - excess_income)
    else:
        if taxable_income <= REBATE_87A_LIMIT_OLD:
            rebate = min(slab_tax, REBATE_87A_MAX_OLD)

    tax_after_rebate = max(ZERO, round_paisa(slab_tax - rebate))
    surcharge = _compute_surcharge(tax_after_rebate, taxable_income, effective_regime)
    cess = round_paisa((tax_after_rebate + surcharge) * HEALTH_EDU_CESS_RATE)
    annual_tax_liability = round_rupee(tax_after_rebate + surcharge + cess)

    return TaxComputationBreakdown(
        regime=effective_regime,
        projected_annual_gross=round_paisa(projected_annual_gross),
        standard_deduction=std_ded,
        professional_tax_exemption=pt_exemption,
        hra_exemption=hra_exemption,
        section_80c_allowed=sec_80c,
        section_80d_allowed=sec_80d,
        section_80ccd_1b_allowed=sec_80ccd_1b,
        section_24b_allowed=sec_24b,
        total_exemptions_and_deductions=total_exemptions,
        taxable_income=taxable_income,
        slab_tax=slab_tax,
        rebate_87a=rebate,
        tax_after_rebate=tax_after_rebate,
        surcharge=surcharge,
        health_and_education_cess=cess,
        annual_tax_liability=annual_tax_liability,
        remaining_months_in_fy=remaining_months_in_fy,
        monthly_tds_computed=monthly_tds_computed,
    )


def get_remaining_months_in_fy(cycle_month: int) -> int:
    """Return number of months left in Indian Financial Year (Apr=12 .. Mar=1)."""
    fy_month_index = (cycle_month - 4) % 12 + 1
    return 13 - fy_month_index


def calculate_monthly_tds(
    *,
    cycle_month: int,
    current_gross: Decimal,
    contractual_monthly_gross: Decimal,
    current_basic_plus_da: Decimal,
    contractual_basic_plus_da: Decimal,
    current_hra: Decimal,
    contractual_hra: Decimal,
    current_pt: Decimal,
    current_epf_ee: Decimal,
    regime: Literal["NEW", "OLD"] = "NEW",
    tax_declaration: Optional[TaxDeclarationInput] = None,
    ytd_gross_earned: Optional[Decimal] = None,
    ytd_basic_plus_da: Optional[Decimal] = None,
    ytd_hra_received: Optional[Decimal] = None,
    ytd_pt_paid: Optional[Decimal] = None,
    ytd_epf_employee: Optional[Decimal] = None,
    ytd_tds_deducted: Decimal = ZERO,
) -> tuple[Decimal, TaxComputationBreakdown]:
    """
    Project annual salary for Indian FY (Apr-Mar) and compute monthly TDS.
      - When `ytd_gross_earned is None` (standalone / single-month run): annualizes
        over 12 months assuming prior FY months earned contractual salary and pays 1/12th.
      - When `ytd_gross_earned is not None` (YTD true-up mode): adds actual YTD +
        current month + remaining months contractual, subtracts YTD TDS, and divides
        by remaining FY months so March settles exact annual liability.
    """
    remaining_months = get_remaining_months_in_fy(cycle_month)
    prior_months = 12 - remaining_months
    future_months = remaining_months - 1

    if ytd_gross_earned is None:
        proj_gross = (
            contractual_monthly_gross * Decimal(prior_months)
            + current_gross
            + contractual_monthly_gross * Decimal(future_months)
        )
        proj_basic_da = (
            contractual_basic_plus_da * Decimal(prior_months)
            + current_basic_plus_da
            + contractual_basic_plus_da * Decimal(future_months)
        )
        proj_hra = (
            contractual_hra * Decimal(prior_months)
            + current_hra
            + contractual_hra * Decimal(future_months)
        )
        proj_pt = current_pt * MONTHS_IN_YEAR
        proj_epf = current_epf_ee * MONTHS_IN_YEAR
        divisor = MONTHS_IN_YEAR
        already_deducted = ZERO
    else:
        proj_gross = (
            ytd_gross_earned
            + current_gross
            + contractual_monthly_gross * Decimal(future_months)
        )
        proj_basic_da = (
            (ytd_basic_plus_da if ytd_basic_plus_da is not None else contractual_basic_plus_da * Decimal(prior_months))
            + current_basic_plus_da
            + contractual_basic_plus_da * Decimal(future_months)
        )
        proj_hra = (
            (ytd_hra_received if ytd_hra_received is not None else contractual_hra * Decimal(prior_months))
            + current_hra
            + contractual_hra * Decimal(future_months)
        )
        proj_pt = (
            (ytd_pt_paid if ytd_pt_paid is not None else current_pt * Decimal(prior_months))
            + current_pt * Decimal(remaining_months)
        )
        proj_epf = (
            (ytd_epf_employee if ytd_epf_employee is not None else current_epf_ee * Decimal(prior_months))
            + current_epf_ee * Decimal(remaining_months)
        )
        divisor = Decimal(remaining_months)
        already_deducted = ytd_tds_deducted

    breakdown = calculate_annual_income_tax(
        projected_annual_gross=proj_gross,
        regime=regime,
        annual_basic_plus_da=proj_basic_da,
        annual_hra_received=proj_hra,
        annual_pt_paid=proj_pt,
        annual_epf_employee=proj_epf,
        tax_declaration=tax_declaration,
        remaining_months_in_fy=remaining_months,
    )

    if current_gross <= ZERO:
        monthly_tds = ZERO
    else:
        remaining_tax = max(ZERO, breakdown.annual_tax_liability - already_deducted)
        monthly_tds = round_rupee(remaining_tax / divisor)

    breakdown = breakdown.model_copy(update={"monthly_tds_computed": monthly_tds})
    return monthly_tds, breakdown


# ---------------------------------------------------------------------------
# Master Orchestration Function
# ---------------------------------------------------------------------------
def calculate_monthly_payroll(emp: PayrollEmployeeInput) -> PayrollCalculationResult:
    """
    Execute the complete, stateless Indian statutory calculation pipeline
    for one employee in one monthly cycle.
    """
    assert emp.total_calendar_days is not None

    # 1. Prorate Earnings (Eliminates DEF-01 Double-Deduction Bug)
    proration = calculate_proration(
        monthly_basic_pay=emp.monthly_basic_pay,
        monthly_hra=emp.monthly_hra,
        monthly_special_allowance=emp.monthly_special_allowance,
        monthly_da=emp.monthly_da,
        total_calendar_days=emp.total_calendar_days,
        unpaid_leave_days=emp.unpaid_leave_days,
        unexcused_absent_days=emp.unexcused_absent_days,
        proration_divisor=emp.proration_divisor,
    )

    # 2. EPF, EPS, EDLI, Admin Charges & Optional VPF
    epf_res = calculate_epf_and_eps(
        earned_basic=proration.earned_basic,
        earned_da=proration.earned_da,
        is_epf_eligible=emp.is_epf_eligible,
        epf_wage_cap_applied=emp.epf_wage_cap_applied,
        is_eps_eligible=emp.is_eps_eligible,
        vpf_percentage=emp.vpf_percentage,
    )

    # 3. ESIC (0.75% EE / 3.25% ER when Gross <= Rs. 21,000)
    esi_res = calculate_esi(
        earned_gross=proration.gross_earnings,
        contractual_monthly_gross=proration.contractual_monthly_gross,
        is_esi_eligible=emp.is_esi_eligible,
    )

    # 4. State Professional Tax
    pt_amount = calculate_professional_tax(
        earned_gross=proration.gross_earnings,
        pt_state_code=emp.pt_state_code,
        cycle_month=emp.cycle_month,
        slabs=emp.pt_slabs,
    )

    # 5. Income Tax TDS (Section 192)
    effective_regime = (
        emp.tax_declaration.regime
        if emp.tax_declaration and emp.tax_declaration.regime
        else emp.tax_regime
    )
    tds_raw, tax_breakdown = calculate_monthly_tds(
        cycle_month=emp.cycle_month,
        current_gross=proration.gross_earnings,
        contractual_monthly_gross=proration.contractual_monthly_gross,
        current_basic_plus_da=proration.earned_basic + proration.earned_da,
        contractual_basic_plus_da=emp.monthly_basic_pay + emp.monthly_da,
        current_hra=proration.earned_hra,
        contractual_hra=emp.monthly_hra,
        current_pt=pt_amount,
        current_epf_ee=epf_res.epf_employee_share + epf_res.vpf_employee_share,
        regime=effective_regime,
        tax_declaration=emp.tax_declaration,
        ytd_gross_earned=emp.ytd_gross_earned,
        ytd_basic_plus_da=emp.ytd_basic_plus_da,
        ytd_hra_received=emp.ytd_hra_received,
        ytd_pt_paid=emp.ytd_pt_paid,
        ytd_epf_employee=emp.ytd_epf_employee,
        ytd_tds_deducted=emp.ytd_tds_deducted,
    )

    # Guard against deducting more TDS than remaining positive earned gross
    pre_tds_statutory = (
        epf_res.epf_employee_share
        + epf_res.vpf_employee_share
        + esi_res.esi_employee_share
        + pt_amount
    )
    available_after_statutory = max(ZERO_PAISA, proration.gross_earnings - pre_tds_statutory)
    tds_amount = min(tds_raw, round_rupee(available_after_statutory))
    if tds_amount != tax_breakdown.monthly_tds_computed:
        tax_breakdown = tax_breakdown.model_copy(update={"monthly_tds_computed": tds_amount})

    # 6. Salary Advance EMI Recovery (capped to positive earned disposable salary)
    available_for_advance = max(
        ZERO_PAISA, proration.gross_earnings - pre_tds_statutory - tds_amount
    )
    advance_recovery = min(
        round_paisa(emp.scheduled_advance_emi),
        round_paisa(available_for_advance),
    )

    # 7. Net Take-Home Pay & Employer Gratuity / Total Cost to Company
    total_employee_deductions = round_paisa(
        epf_res.epf_employee_share
        + epf_res.vpf_employee_share
        + esi_res.esi_employee_share
        + pt_amount
        + tds_amount
        + advance_recovery
    )
    reimbursements = round_paisa(emp.approved_reimbursements)
    net_payable = round_paisa(
        proration.gross_earnings - total_employee_deductions + reimbursements
    )

    gratuity_accrual = calculate_gratuity_accrual(
        earned_basic=proration.earned_basic,
        earned_da=proration.earned_da,
    )
    total_employer_cost = round_paisa(
        proration.gross_earnings
        + epf_res.eps_employer_share
        + epf_res.epf_employer_share
        + epf_res.edli_employer_share
        + epf_res.epf_admin_charges
        + esi_res.esi_employer_share
        + gratuity_accrual
    )

    # 8. Immutable JSONB Snapshots (convertible directly to JSON for audit storage)
    earnings_breakdown: dict[str, Any] = {
        "basic": float(proration.earned_basic),
        "hra": float(proration.earned_hra),
        "special_allowance": float(proration.earned_special_allowance),
        "da": float(proration.earned_da),
        "gross_earnings": float(proration.gross_earnings),
        "reimbursements": float(reimbursements),
        "contractual_gross": float(proration.contractual_monthly_gross),
        "lop_deduction_informational": float(proration.lop_deduction_informational),
    }
    deductions_breakdown: dict[str, Any] = {
        "epf_employee": float(epf_res.epf_employee_share),
        "vpf_employee": float(epf_res.vpf_employee_share),
        "esi_employee": float(esi_res.esi_employee_share),
        "professional_tax": float(pt_amount),
        "tds": float(tds_amount),
        "advance_recovery": float(advance_recovery),
        "total_deductions": float(total_employee_deductions),
        "employer_contributions": {
            "eps": float(epf_res.eps_employer_share),
            "epf": float(epf_res.epf_employer_share),
            "edli": float(epf_res.edli_employer_share),
            "epf_admin": float(epf_res.epf_admin_charges),
            "esi": float(esi_res.esi_employer_share),
            "gratuity_accrual": float(gratuity_accrual),
        },
        "tax_regime": tax_breakdown.regime,
        "taxable_income": float(tax_breakdown.taxable_income),
    }

    return PayrollCalculationResult(
        employee_id=emp.employee_id,
        employee_name=emp.employee_name,
        cycle_month=emp.cycle_month,
        cycle_year=emp.cycle_year,
        total_calendar_days=proration.total_calendar_days,
        days_worked=proration.days_worked,
        unpaid_leave_days=proration.unpaid_leave_days,
        unexcused_absent_days=proration.unexcused_absent_days,
        ncp_days=proration.ncp_days,
        earned_basic=proration.earned_basic,
        earned_hra=proration.earned_hra,
        earned_special_allowance=proration.earned_special_allowance,
        earned_da=proration.earned_da,
        gross_earnings=proration.gross_earnings,
        epf_wages=epf_res.epf_wages,
        eps_wages=epf_res.eps_wages,
        edli_wages=epf_res.edli_wages,
        esi_wages=esi_res.esi_wages,
        epf_employee_share=epf_res.epf_employee_share,
        vpf_employee_share=epf_res.vpf_employee_share,
        eps_employer_share=epf_res.eps_employer_share,
        epf_employer_share=epf_res.epf_employer_share,
        edli_employer_share=epf_res.edli_employer_share,
        epf_admin_charges=epf_res.epf_admin_charges,
        esi_employee_share=esi_res.esi_employee_share,
        esi_employer_share=esi_res.esi_employer_share,
        professional_tax=pt_amount,
        tds_amount=tds_amount,
        reimbursements_amount=reimbursements,
        advance_recovery_amount=advance_recovery,
        total_employee_deductions=total_employee_deductions,
        net_payable=net_payable,
        gratuity_accrual_monthly=gratuity_accrual,
        total_employer_cost=total_employer_cost,
        lop_deduction_informational=proration.lop_deduction_informational,
        earnings_breakdown=earnings_breakdown,
        deductions_breakdown=deductions_breakdown,
        tax_breakdown=tax_breakdown,
    )
