"""
Comprehensive Unit & Compliance Test Suite for `engine_india.py` (Payroll V2).

Covers:
  - Seeded Aimploy sample employees (Darryl M A, Ajmal Asharudeen, Damewan Bareh)
  - 28/29/30/31-day proration boundaries & DEF-01 double-deduction regression guard
  - EPF / EPS / EDLI / VPF & EPFO ECR checksum invariant (EE == EPS + ER)
  - ESIC eligibility threshold (<= Rs. 21,000 contractual gross) & rounding
  - State PT slabs (KA, MH incl. Feb Rs. 300, TS, DL, custom DB slabs)
  - Payment of Gratuity 15/26 accrual formula
  - Income Tax Act Sec 192: New Regime Sec 115BAC (Sec 87A + marginal relief),
    Old Regime (HRA 10(13A), PT 16(iii), 80C/80D/80CCD(1B)/24(b)), Surcharge,
    Health & Education Cess (4%), and 12-month Apr-Mar YTD reconciliation
  - Approved reimbursements, salary advance EMI cap (non-negative net pay), and
    AGENT.md Rule 3 isolation guard (zero DB or network imports)
"""

from __future__ import annotations

import ast
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.features.payroll_v2.engine_india import (
    PTSlab,
    PayrollEmployeeInput,
    TaxDeclarationInput,
    calculate_annual_income_tax,
    calculate_epf_and_eps,
    calculate_esi,
    calculate_gratuity_accrual,
    calculate_hra_exemption,
    calculate_monthly_payroll,
    calculate_professional_tax,
    calculate_proration,
    get_remaining_months_in_fy,
)


# ---------------------------------------------------------------------------
# 1. End-to-End Tests for Seeded Employees (015 Schema Reference Profiles)
# ---------------------------------------------------------------------------
class TestSeededEmployeesEndToEnd:
    def test_darryl_12l_ctc_new_regime_ka(self):
        """Darryl M A: Rs. 12L CTC, Rs. 1L gross, capped EPF, KA PT, New Regime TDS."""
        emp = PayrollEmployeeInput(
            employee_id="c0ede84e-56f9-48f8-a133-0cb68d618be7",
            employee_name="Darryl M A",
            cycle_month=11,
            cycle_year=2026,
            monthly_basic_pay=Decimal("50000"),
            monthly_hra=Decimal("25000"),
            monthly_special_allowance=Decimal("25000"),
            is_epf_eligible=True,
            epf_wage_cap_applied=True,
            is_esi_eligible=False,
            pt_state_code="KA",
            tax_regime="NEW",
        )
        res = calculate_monthly_payroll(emp)

        assert res.gross_earnings == Decimal("100000.00")
        assert res.ncp_days == Decimal("0")
        assert res.days_worked == Decimal("30")

        # EPF / EPS / EDLI (capped at Rs. 15,000)
        assert res.epf_wages == Decimal("15000")
        assert res.eps_wages == Decimal("15000")
        assert res.edli_wages == Decimal("15000")
        assert res.epf_employee_share == Decimal("1800")
        assert res.eps_employer_share == Decimal("1250")
        assert res.epf_employer_share == Decimal("550")
        assert res.edli_employer_share == Decimal("75")
        assert res.epf_admin_charges == Decimal("75")

        # ESI exempt
        assert res.esi_wages == Decimal("0.00")
        assert res.esi_employee_share == Decimal("0")
        assert res.esi_employer_share == Decimal("0")

        # Karnataka PT
        assert res.professional_tax == Decimal("200")

        # New Regime TDS: Gross 12,00,000 - Std Ded 75,000 = 11,25,000 taxable
        # Slab tax = 0 + 20,000 + 30,000 + 18,750 = 68,750; Cess 4% = 2,750; Annual = 71,500
        # Monthly = 71,500 / 12 = 5,958.33 -> Rs. 5,958
        assert res.tax_breakdown.taxable_income == Decimal("1125000.00")
        assert res.tax_breakdown.annual_tax_liability == Decimal("71500")
        assert res.tds_amount == Decimal("5958")

        assert res.total_employee_deductions == Decimal("7958.00")
        assert res.net_payable == Decimal("92042.00")

        # Employer Gratuity (50,000 * 15 / 26 / 12 = 2403.85) & CTC
        assert res.gratuity_accrual_monthly == Decimal("2403.85")
        assert res.total_employer_cost == Decimal("104353.85")

        # JSONB snapshots must be JSON serializable
        json.dumps(res.earnings_breakdown)
        json.dumps(res.deductions_breakdown)

    def test_ajmal_4_8l_ctc_sec_87a_zero_tds(self):
        """Ajmal Asharudeen: Rs. 4.8L CTC, Rs. 40k gross, zero TDS via Sec 87A."""
        emp = PayrollEmployeeInput(
            employee_id="3c0b28d6-a1aa-4feb-9a90-12d4f9c3dd58",
            employee_name="Ajmal Asharudeen",
            cycle_month=11,
            cycle_year=2026,
            monthly_basic_pay=Decimal("20000"),
            monthly_hra=Decimal("10000"),
            monthly_special_allowance=Decimal("10000"),
            is_epf_eligible=True,
            epf_wage_cap_applied=True,
            is_esi_eligible=False,
            pt_state_code="KA",
            tax_regime="NEW",
        )
        res = calculate_monthly_payroll(emp)

        assert res.gross_earnings == Decimal("40000.00")
        assert res.epf_employee_share == Decimal("1800")
        assert res.eps_employer_share == Decimal("1250")
        assert res.epf_employer_share == Decimal("550")
        assert res.esi_employee_share == Decimal("0")
        assert res.professional_tax == Decimal("200")
        assert res.tds_amount == Decimal("0")
        assert res.net_payable == Decimal("38000.00")
        assert res.gratuity_accrual_monthly == Decimal("961.54")

    def test_damewan_2_4l_ctc_esi_eligible_maharashtra_regular_and_feb(self):
        """Damewan Bareh: Rs. 2.4L CTC, Rs. 20k gross, ESI 0.75%/3.25%, MH PT incl. Feb."""
        base_kwargs = dict(
            employee_id="dff404dd-2b83-4fd9-b34e-aa65707377e0",
            employee_name="Damewan Bareh",
            monthly_basic_pay=Decimal("10000"),
            monthly_hra=Decimal("5000"),
            monthly_special_allowance=Decimal("5000"),
            is_epf_eligible=True,
            epf_wage_cap_applied=True,
            is_esi_eligible=True,
            pt_state_code="MH",
            tax_regime="NEW",
        )

        # Regular month (November 2026) -> MH PT = Rs. 200
        nov = calculate_monthly_payroll(
            PayrollEmployeeInput(cycle_month=11, cycle_year=2026, **base_kwargs)
        )
        assert nov.gross_earnings == Decimal("20000.00")
        assert nov.epf_wages == Decimal("10000")
        assert nov.epf_employee_share == Decimal("1200")
        assert nov.eps_employer_share == Decimal("833")
        assert nov.epf_employer_share == Decimal("367")
        assert nov.edli_employer_share == Decimal("50")
        assert nov.epf_admin_charges == Decimal("50")
        assert nov.esi_employee_share == Decimal("150")
        assert nov.esi_employer_share == Decimal("650")
        assert nov.professional_tax == Decimal("200")
        assert nov.tds_amount == Decimal("0")
        assert nov.net_payable == Decimal("18450.00")
        assert nov.gratuity_accrual_monthly == Decimal("480.77")

        # February (Feb 2027, 28 days, 0 LOP) -> MH Feb PT = Rs. 300
        feb = calculate_monthly_payroll(
            PayrollEmployeeInput(cycle_month=2, cycle_year=2027, **base_kwargs)
        )
        assert feb.total_calendar_days == 28
        assert feb.days_worked == Decimal("28")
        assert feb.gross_earnings == Decimal("20000.00")
        assert feb.professional_tax == Decimal("300")
        assert feb.net_payable == Decimal("18350.00")


# ---------------------------------------------------------------------------
# 2. Proration & DEF-01 Double-Deduction Regression Tests
# ---------------------------------------------------------------------------
class TestProrationAndDoubleDeductionRegression:
    def test_def01_no_double_deduction_on_unpaid_leave(self):
        """
        DEF-01 guard: 3 unpaid days out of 30 on Rs. 40,000 gross
        must yield Rs. 36,000 earned gross and Rs. 34,000 net (NOT Rs. 30,000).
        """
        emp = PayrollEmployeeInput(
            cycle_month=11,
            cycle_year=2026,
            monthly_basic_pay=Decimal("20000"),
            monthly_hra=Decimal("10000"),
            monthly_special_allowance=Decimal("10000"),
            unpaid_leave_days=Decimal("2"),
            unexcused_absent_days=Decimal("1"),
            pt_state_code="KA",
            is_esi_eligible=False,
        )
        res = calculate_monthly_payroll(emp)

        assert res.ncp_days == Decimal("3")
        assert res.days_worked == Decimal("27")
        assert res.earned_basic == Decimal("18000.00")
        assert res.earned_hra == Decimal("9000.00")
        assert res.earned_special_allowance == Decimal("9000.00")
        assert res.gross_earnings == Decimal("36000.00")
        assert res.lop_deduction_informational == Decimal("4000.00")

        # Earned Basic 18k still exceeds 15k ceiling -> PF = 1800, PT = 200
        assert res.epf_employee_share == Decimal("1800")
        assert res.professional_tax == Decimal("200")
        assert res.total_employee_deductions == Decimal("2000.00")
        assert res.net_payable == Decimal("34000.00")

    @pytest.mark.parametrize(
        "year,month,expected_cal_days",
        [
            (2026, 2, 28),
            (2024, 2, 29),
            (2026, 4, 30),
            (2026, 5, 31),
        ],
    )
    def test_zero_ncp_pays_100_percent_across_all_month_lengths(
        self, year, month, expected_cal_days
    ):
        emp = PayrollEmployeeInput(
            cycle_month=month,
            cycle_year=year,
            monthly_basic_pay=Decimal("30000"),
            monthly_hra=Decimal("15000"),
            monthly_special_allowance=Decimal("15000"),
            is_esi_eligible=False,
        )
        res = calculate_monthly_payroll(emp)
        assert res.total_calendar_days == expected_cal_days
        assert res.days_worked == Decimal(expected_cal_days)
        assert res.gross_earnings == Decimal("60000.00")

    def test_worked_single_day_in_31_day_month_pays_positive(self):
        """30 NCP days in a 31-day month leaves 1 worked day -> 1/30th pay (not Rs. 0 or negative)."""
        proration = calculate_proration(
            monthly_basic_pay=Decimal("30000"),
            monthly_hra=Decimal("15000"),
            monthly_special_allowance=Decimal("15000"),
            monthly_da=Decimal("0"),
            total_calendar_days=31,
            unpaid_leave_days=Decimal("30"),
        )
        assert proration.days_worked == Decimal("1")
        assert proration.ncp_days == Decimal("30")
        assert proration.earned_basic == Decimal("1000.00")
        assert proration.gross_earnings == Decimal("2000.00")

    def test_worked_single_day_in_feb_does_not_overpay_phantom_days(self):
        """27 NCP days in 28-day Feb leaves 1 worked day -> pays 1/30th (not 3/30ths)."""
        proration = calculate_proration(
            monthly_basic_pay=Decimal("30000"),
            monthly_hra=Decimal("0"),
            monthly_special_allowance=Decimal("0"),
            monthly_da=Decimal("0"),
            total_calendar_days=28,
            unpaid_leave_days=Decimal("27"),
        )
        assert proration.days_worked == Decimal("1")
        assert proration.gross_earnings == Decimal("1000.00")

    def test_full_month_absence_yields_exact_zero_everything(self):
        for year, month, days in [(2026, 2, 28), (2026, 11, 30), (2026, 5, 31)]:
            emp = PayrollEmployeeInput(
                cycle_month=month,
                cycle_year=year,
                monthly_basic_pay=Decimal("50000"),
                monthly_hra=Decimal("25000"),
                monthly_special_allowance=Decimal("25000"),
                unpaid_leave_days=Decimal(days),
            )
            res = calculate_monthly_payroll(emp)
            assert res.days_worked == Decimal("0")
            assert res.gross_earnings == Decimal("0.00")
            assert res.epf_employee_share == Decimal("0")
            assert res.eps_employer_share == Decimal("0")
            assert res.esi_employee_share == Decimal("0")
            assert res.professional_tax == Decimal("0")
            assert res.tds_amount == Decimal("0")
            assert res.gratuity_accrual_monthly == Decimal("0.00")
            assert res.net_payable == Decimal("0.00")

    def test_february_proration_monotonicity_sweep(self):
        """Earned gross must monotonically non-increase as NCP grows from 0 to 28."""
        prev_gross = Decimal("999999")
        for ncp in range(0, 29):
            p = calculate_proration(
                monthly_basic_pay=Decimal("30000"),
                monthly_hra=Decimal("15000"),
                monthly_special_allowance=Decimal("15000"),
                monthly_da=Decimal("0"),
                total_calendar_days=28,
                unpaid_leave_days=Decimal(ncp),
            )
            assert Decimal("0") <= p.gross_earnings <= prev_gross
            prev_gross = p.gross_earnings
        assert prev_gross == Decimal("0.00")

    def test_fractional_ncp_days_half_day_lop(self):
        p = calculate_proration(
            monthly_basic_pay=Decimal("30000"),
            monthly_hra=Decimal("0"),
            monthly_special_allowance=Decimal("0"),
            monthly_da=Decimal("0"),
            total_calendar_days=30,
            unpaid_leave_days=Decimal("1.5"),
        )
        assert p.days_worked == Decimal("28.5")
        assert p.gross_earnings == Decimal("28500.00")

    def test_ncp_exceeding_calendar_days_raises_validation_error(self):
        with pytest.raises(ValueError, match="Total NCP days"):
            PayrollEmployeeInput(
                cycle_month=11,
                cycle_year=2026,
                monthly_basic_pay=Decimal("20000"),
                unpaid_leave_days=Decimal("20"),
                unexcused_absent_days=Decimal("11"),
            )


# ---------------------------------------------------------------------------
# 3. EPF / EPS / EDLI / VPF Rules & ECR Checksum Tests
# ---------------------------------------------------------------------------
class TestEPFAndEPSStatutoryRules:
    def test_uncapped_epf_caps_eps_at_1250_and_edli_at_75(self):
        res = calculate_epf_and_eps(
            earned_basic=Decimal("50000"),
            is_epf_eligible=True,
            epf_wage_cap_applied=False,
            is_eps_eligible=True,
        )
        assert res.epf_wages == Decimal("50000")
        assert res.eps_wages == Decimal("15000")
        assert res.edli_wages == Decimal("15000")
        assert res.epf_employee_share == Decimal("6000")
        assert res.eps_employer_share == Decimal("1250")
        assert res.epf_employer_share == Decimal("4750")
        assert res.edli_employer_share == Decimal("75")
        assert res.epf_admin_charges == Decimal("250")
        assert res.epf_employee_share == res.eps_employer_share + res.epf_employer_share

    def test_dearness_allowance_included_in_epf_wages(self):
        res = calculate_epf_and_eps(
            earned_basic=Decimal("10000"),
            earned_da=Decimal("4000"),
            is_epf_eligible=True,
            epf_wage_cap_applied=True,
        )
        assert res.epf_wages == Decimal("14000")
        assert res.epf_employee_share == Decimal("1680")
        assert res.eps_employer_share == Decimal("1166")
        assert res.epf_employer_share == Decimal("514")

    def test_non_eps_member_routes_all_12pct_to_employer_epf(self):
        res = calculate_epf_and_eps(
            earned_basic=Decimal("50000"),
            is_epf_eligible=True,
            epf_wage_cap_applied=True,
            is_eps_eligible=False,
        )
        assert res.eps_wages == Decimal("0")
        assert res.eps_employer_share == Decimal("0")
        assert res.epf_employer_share == Decimal("1800")

    def test_epf_ineligible_returns_zero_shares(self):
        res = calculate_epf_and_eps(
            earned_basic=Decimal("50000"),
            is_epf_eligible=False,
        )
        assert res.epf_employee_share == Decimal("0")
        assert res.eps_employer_share == Decimal("0")
        assert res.epf_employer_share == Decimal("0")
        assert res.edli_employer_share == Decimal("0")
        assert res.epf_admin_charges == Decimal("0")

    def test_voluntary_pf_deducted_only_from_employee(self):
        res = calculate_epf_and_eps(
            earned_basic=Decimal("50000"),
            is_epf_eligible=True,
            epf_wage_cap_applied=True,
            vpf_percentage=Decimal("10"),
        )
        assert res.epf_employee_share == Decimal("1800")
        assert res.vpf_employee_share == Decimal("5000")
        assert res.eps_employer_share == Decimal("1250")
        assert res.epf_employer_share == Decimal("550")

    def test_epfo_ecr_checksum_invariant_sweep(self):
        """EE Share must ALWAYS equal EPS Share + ER Share across all salary levels."""
        for basic in range(1000, 60001, 137):
            for capped in (True, False):
                res = calculate_epf_and_eps(
                    earned_basic=Decimal(basic),
                    is_epf_eligible=True,
                    epf_wage_cap_applied=capped,
                )
                assert res.epf_employee_share == res.eps_employer_share + res.epf_employer_share
                assert res.eps_employer_share <= Decimal("1250")
                assert res.edli_employer_share <= Decimal("75")


# ---------------------------------------------------------------------------
# 4. ESIC Threshold & Rounding Rules
# ---------------------------------------------------------------------------
class TestESICStatutoryRules:
    def test_esi_boundary_exactly_21000_is_eligible(self):
        res = calculate_esi(
            earned_gross=Decimal("21000"),
            contractual_monthly_gross=Decimal("21000"),
        )
        assert res.is_esi_eligible is True
        # 21000 * 0.0075 = 157.50 -> 158 (ROUND_HALF_UP)
        # 21000 * 0.0325 = 682.50 -> 683 (ROUND_HALF_UP)
        assert res.esi_employee_share == Decimal("158")
        assert res.esi_employer_share == Decimal("683")

    def test_esi_above_21000_is_exempt(self):
        res = calculate_esi(
            earned_gross=Decimal("21001"),
            contractual_monthly_gross=Decimal("21001"),
        )
        assert res.is_esi_eligible is False
        assert res.esi_employee_share == Decimal("0")
        assert res.esi_employer_share == Decimal("0")

    def test_lop_does_not_trigger_esi_for_high_earner(self):
        """Employee with Rs. 40k contractual gross taking 20d LOP remains ESI-exempt."""
        res = calculate_esi(
            earned_gross=Decimal("13333.33"),
            contractual_monthly_gross=Decimal("40000"),
        )
        assert res.is_esi_eligible is False
        assert res.esi_employee_share == Decimal("0")

    def test_esi_explicit_true_override_for_mid_period_increment(self):
        res = calculate_esi(
            earned_gross=Decimal("22000"),
            contractual_monthly_gross=Decimal("22000"),
            is_esi_eligible=True,
        )
        assert res.is_esi_eligible is True
        assert res.esi_employee_share == Decimal("165")
        assert res.esi_employer_share == Decimal("715")

    def test_esi_establishment_gate_uncovered_returns_zero(self):
        """When establishment is not covered under ESI Act (< 10 workers / exempt), ESI is 0 even if gross <= 21k."""
        res = calculate_esi(
            earned_gross=Decimal("15000"),
            contractual_monthly_gross=Decimal("15000"),
            is_company_esi_covered=False,
        )
        assert res.is_esi_eligible is False
        assert res.esi_employee_share == Decimal("0")
        assert res.esi_employer_share == Decimal("0")
        assert res.esi_wages == Decimal("0.00")


# ---------------------------------------------------------------------------
# 5. State Professional Tax Slabs
# ---------------------------------------------------------------------------
class TestProfessionalTaxStateSlabs:
    @pytest.mark.parametrize(
        "state,gross,month,expected_pt",
        [
            ("KA", "15000", 11, "0"),
            ("KA", "15001", 11, "200"),
            ("MH", "7500", 11, "0"),
            ("MH", "9000", 11, "175"),
            ("MH", "20000", 11, "200"),
            ("MH", "20000", 2, "300"),
            ("TS", "15000", 11, "0"),
            ("TS", "18000", 11, "150"),
            ("TS", "25000", 11, "200"),
            ("DL", "100000", 2, "0"),
        ],
    )
    def test_state_slabs(self, state, gross, month, expected_pt):
        pt = calculate_professional_tax(
            earned_gross=Decimal(gross),
            pt_state_code=state,
            cycle_month=month,
        )
        assert pt == Decimal(expected_pt)

    def test_custom_pt_slabs_override(self):
        custom = [
            PTSlab(
                state_code="KA",
                state_name="Karnataka",
                min_monthly_gross=Decimal("0"),
                max_monthly_gross=Decimal("24999.99"),
                pt_amount=Decimal("0"),
            ),
            PTSlab(
                state_code="KA",
                state_name="Karnataka",
                min_monthly_gross=Decimal("25000"),
                max_monthly_gross=None,
                pt_amount=Decimal("200"),
            ),
        ]
        assert (
            calculate_professional_tax(
                earned_gross=Decimal("20000"),
                pt_state_code="KA",
                cycle_month=11,
                slabs=custom,
            )
            == Decimal("0")
        )
        assert (
            calculate_professional_tax(
                earned_gross=Decimal("25000"),
                pt_state_code="KA",
                cycle_month=11,
                slabs=custom,
            )
            == Decimal("200")
        )


# ---------------------------------------------------------------------------
# 6. Gratuity & Income Tax Sec 192 Tests (New, Old, Surcharge, YTD)
# ---------------------------------------------------------------------------
class TestGratuityAndIncomeTax:
    def test_gratuity_15_by_26_formula(self):
        assert calculate_gratuity_accrual(earned_basic=Decimal("50000")) == Decimal("2403.85")
        assert calculate_gratuity_accrual(
            earned_basic=Decimal("20000"), earned_da=Decimal("6000")
        ) == Decimal("1250.00")

    def test_new_regime_87a_full_rebate_and_marginal_relief(self):
        # Taxable = 7,00,000 -> Full rebate
        t1 = calculate_annual_income_tax(
            projected_annual_gross=Decimal("775000"),
            regime="NEW",
        )
        assert t1.taxable_income == Decimal("700000.00")
        assert t1.annual_tax_liability == Decimal("0")

        # Taxable = 7,10,000 -> Slab tax 21,000, Marginal relief caps pre-cess tax to 10,000 -> 10,400 w/ cess
        t2 = calculate_annual_income_tax(
            projected_annual_gross=Decimal("785000"),
            regime="NEW",
        )
        assert t2.taxable_income == Decimal("710000.00")
        assert t2.tax_after_rebate == Decimal("10000.00")
        assert t2.health_and_education_cess == Decimal("400.00")
        assert t2.annual_tax_liability == Decimal("10400")

    def test_new_regime_top_slab_24l_gross(self):
        t = calculate_annual_income_tax(
            projected_annual_gross=Decimal("2400000"),
            regime="NEW",
        )
        # Taxable = 23,25,000
        # 0-3L: 0; 3-7L: 20k; 7-10L: 30k; 10-12L: 30k; 12-15L: 60k; 15-23.25L (8.25L @ 30%): 2,47,500
        # Slab tax = 3,87,500; Cess 4% = 15,500; Total = 4,03,000
        assert t.taxable_income == Decimal("2325000.00")
        assert t.slab_tax == Decimal("387500.00")
        assert t.annual_tax_liability == Decimal("403000")

    def test_hra_exemption_and_old_regime_caps(self):
        # Metro vs Non-Metro HRA check
        # Basic = 6,00,000; HRA = 3,00,000; Rent = 2,40,000
        # Rent - 10% Basic = 2,40,000 - 60,000 = 1,80,000 (least!)
        hra_metro = calculate_hra_exemption(
            annual_basic_plus_da=Decimal("600000"),
            annual_hra_received=Decimal("300000"),
            annual_rent_paid=Decimal("240000"),
            is_metro=True,
        )
        assert hra_metro == Decimal("180000")

        # High rent 4,00,000 -> Non-Metro caps at 40% Basic (2,40,000) vs Metro 50% (3,00,000)
        hra_non_metro = calculate_hra_exemption(
            annual_basic_plus_da=Decimal("600000"),
            annual_hra_received=Decimal("300000"),
            annual_rent_paid=Decimal("400000"),
            is_metro=False,
        )
        assert hra_non_metro == Decimal("240000")

        # Old Regime with caps exceeding statutory limits
        decl = TaxDeclarationInput(
            regime="OLD",
            section_80c_amount=Decimal("250000"),   # Capped to 1,50,000
            section_80d_amount=Decimal("25000"),
            annual_rent_paid=Decimal("240000"),
            home_loan_interest=Decimal("250000"),   # Capped to 2,00,000
            nps_80ccd_1b=Decimal("80000"),          # Capped to 50,000
            is_metro=True,
        )
        old_tax = calculate_annual_income_tax(
            projected_annual_gross=Decimal("1500000"),
            regime="OLD",
            annual_basic_plus_da=Decimal("600000"),
            annual_hra_received=Decimal("300000"),
            annual_pt_paid=Decimal("2400"),
            tax_declaration=decl,
        )
        # Allowed deductions: 50,000 std + 2,400 PT + 1,80,000 HRA + 1,50,000 80C + 25,000 80D + 50,000 80CCD + 2,00,000 24b = 6,57,400
        assert old_tax.section_80c_allowed == Decimal("150000")
        assert old_tax.section_80ccd_1b_allowed == Decimal("50000")
        assert old_tax.section_24b_allowed == Decimal("200000")
        assert old_tax.hra_exemption == Decimal("180000")
        assert old_tax.professional_tax_exemption == Decimal("2400")
        assert old_tax.taxable_income == Decimal("842600.00")
        # Old slabs: 0-2.5L: 0; 2.5-5L: 12,500; 5-8.426L (3,42,600 @ 20%): 68,520 = 81,020
        assert old_tax.slab_tax == Decimal("81020.00")
        assert old_tax.annual_tax_liability == Decimal("84261")

    def test_high_income_surcharge_above_50_lakhs(self):
        # Taxable 60,00,000 under New Regime -> 10% surcharge applies
        t = calculate_annual_income_tax(
            projected_annual_gross=Decimal("6075000"),
            regime="NEW",
        )
        assert t.taxable_income == Decimal("6000000.00")
        # Base tax on 60L: 20k + 30k + 30k + 60k + 45L*0.30 (13,50,000) = 14,90,000
        assert t.slab_tax == Decimal("1490000.00")
        assert t.surcharge == Decimal("149000.00")
        # Cess = 4% of (14,90,000 + 1,49,000 = 16,39,000) = 65,560
        assert t.health_and_education_cess == Decimal("65560.00")
        assert t.annual_tax_liability == Decimal("1704560")

    def test_fy_remaining_months_mapping(self):
        assert get_remaining_months_in_fy(4) == 12
        assert get_remaining_months_in_fy(11) == 5
        assert get_remaining_months_in_fy(12) == 4
        assert get_remaining_months_in_fy(1) == 3
        assert get_remaining_months_in_fy(3) == 1

    def test_full_12_month_fy_ytd_reconciliation(self):
        """
        Simulate all 12 months (Apr 2026 .. Mar 2027) for Darryl using cumulative YTD inputs.
        Total TDS withheld across the FY must equal exact annual liability (Rs. 71,500).
        """
        fy_schedule = [
            (4, 2026), (5, 2026), (6, 2026), (7, 2026), (8, 2026), (9, 2026),
            (10, 2026), (11, 2026), (12, 2026), (1, 2027), (2, 2027), (3, 2027),
        ]
        ytd_gross = Decimal("0")
        ytd_tds = Decimal("0")
        for month, year in fy_schedule:
            res = calculate_monthly_payroll(
                PayrollEmployeeInput(
                    cycle_month=month,
                    cycle_year=year,
                    monthly_basic_pay=Decimal("50000"),
                    monthly_hra=Decimal("25000"),
                    monthly_special_allowance=Decimal("25000"),
                    ytd_gross_earned=ytd_gross,
                    ytd_tds_deducted=ytd_tds,
                )
            )
            ytd_gross += res.gross_earnings
            ytd_tds += res.tds_amount

        assert ytd_gross == Decimal("1200000.00")
        assert ytd_tds == Decimal("71500")


# ---------------------------------------------------------------------------
# 7. Adjustments & Architectural Boundary Guards
# ---------------------------------------------------------------------------
class TestAdjustmentsAndIsolationGuards:
    def test_reimbursements_pass_through_tax_free_and_pf_free(self):
        emp = PayrollEmployeeInput(
            cycle_month=11,
            cycle_year=2026,
            monthly_basic_pay=Decimal("10000"),
            monthly_hra=Decimal("5000"),
            monthly_special_allowance=Decimal("5000"),
            pt_state_code="MH",
            approved_reimbursements=Decimal("4500.50"),
        )
        res = calculate_monthly_payroll(emp)
        # Reimbursements must NOT push Rs. 20k salary over Rs. 21k ESI ceiling or affect PF/PT
        assert res.esi_employee_share == Decimal("150")
        assert res.epf_employee_share == Decimal("1200")
        assert res.professional_tax == Decimal("200")
        assert res.net_payable == Decimal("22950.50")

    def test_salary_advance_emi_deducted_and_never_forces_negative_pay(self):
        # Normal month: full Rs. 5,000 EMI recovered
        normal = calculate_monthly_payroll(
            PayrollEmployeeInput(
                cycle_month=11,
                cycle_year=2026,
                monthly_basic_pay=Decimal("20000"),
                monthly_hra=Decimal("10000"),
                monthly_special_allowance=Decimal("10000"),
                scheduled_advance_emi=Decimal("5000"),
            )
        )
        assert normal.advance_recovery_amount == Decimal("5000.00")
        assert normal.net_payable == Decimal("33000.00")

        # Extreme LOP month: 1 day worked on Rs. 30k gross -> earned Rs. 1,000, PF ~72, EMI capped <= available
        near_zero = calculate_monthly_payroll(
            PayrollEmployeeInput(
                cycle_month=11,
                cycle_year=2026,
                monthly_basic_pay=Decimal("15000"),
                monthly_hra=Decimal("7500"),
                monthly_special_allowance=Decimal("7500"),
                unpaid_leave_days=Decimal("29"),
                scheduled_advance_emi=Decimal("5000"),
            )
        )
        assert near_zero.net_payable == Decimal("0.00")
        assert near_zero.advance_recovery_amount < Decimal("5000.00")

    def test_engine_has_zero_db_or_network_imports(self):
        """Enforce AGENT.md Rule 3: Pure Math Principle (zero DB or network dependencies)."""
        engine_path = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "features"
            / "payroll_v2"
            / "engine_india.py"
        )
        tree = ast.parse(engine_path.read_text(encoding="utf-8"))
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module.split(".")[0])

        forbidden = {
            "asyncpg",
            "psycopg",
            "psycopg2",
            "sqlalchemy",
            "fastapi",
            "httpx",
            "requests",
            "urllib",
            "aiohttp",
            "redis",
            "celery",
            "supabase",
            "app",
        }
        violations = imported_modules.intersection(forbidden)
        assert not violations, f"Forbidden I/O or app imports in pure engine: {violations}"
