"""
Offline Unit & Integration Tests for Phase 3 Payroll V2 Adapters.

Runs 100% locally in memory using mock asyncpg connections — ZERO live calls
to Neon Cloud PostgreSQL. Verifies:
  - Indian Calendar Service (Sat/Sun defaults, holidays, DEF-03 prevention)
  - Leave Adapter (paid vs unpaid, half-day, weekend skipping, month boundaries)
  - Attendance Adapter (IST timezone SQL, regularisations, cutoff/as_of, joining/exit)
  - Expense Adapter (strict `approved` + `paid_at IS NULL` + `expense_date <= cutoff`)
  - Advance Adapter (EMI capped at `remaining_balance`)
  - Full Adapter -> `engine_india.calculate_monthly_payroll()` pipeline
  - Read-only SQL security audit (zero writes/mutations in adapter queries)
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from app.features.payroll_v2.adapters import (
    AdvanceLoanRow,
    ApprovedReimbursementItem,
    LeaveRecord,
    aggregate_employee_expenses,
    aggregate_employee_leaves,
    build_indian_calendar_summary,
    compute_advance_summary,
    compute_attendance_summary,
    fetch_advances_batch,
    fetch_attendance_batch,
    fetch_cycle_employee_inputs,
    fetch_indian_calendar,
    fetch_leaves_batch,
    fetch_reimbursements_batch,
    get_financial_year_label,
    parse_weekend_days,
    select_latest_pt_slabs,
)
from app.features.payroll_v2.engine_india import calculate_monthly_payroll


# ---------------------------------------------------------------------------
# Mock Asyncpg Connection (In-Memory Query Dispatcher & SQL Auditor)
# ---------------------------------------------------------------------------
class MockAsyncpgConn:
    """Captures every executed SQL statement and dispatches mock rows cleanly."""

    def __init__(self, handlers: dict[str, Any]):
        self.handlers = handlers
        self.executed_queries: list[tuple[str, tuple[Any, ...]]] = []

    def _match(self, sql: str, *args: Any) -> Any:
        self.executed_queries.append((sql, args))
        normalized = " ".join(sql.split()).lower()
        for needle, response in self.handlers.items():
            if needle.lower() in normalized:
                return response(args) if callable(response) else response
        raise AssertionError(f"Unexpected SQL in test: {normalized}")

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self._match(sql, *args)

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return self._match(sql, *args)

    async def execute(self, sql: str, *args: Any) -> Any:
        raise AssertionError(f"FORBIDDEN write operation in read-only adapter: {sql}")


# ---------------------------------------------------------------------------
# 1. Indian Calendar Service Tests (DEF-03 Prevention)
# ---------------------------------------------------------------------------
class TestIndianCalendarService:
    def test_parse_weekend_days_defaults_to_sat_sun(self):
        assert parse_weekend_days("Saturday,Sunday") == {5, 6}
        assert parse_weekend_days(None) == {5, 6}
        assert parse_weekend_days("") == {5, 6}
        assert parse_weekend_days("Sunday") == {6}

    def test_november_2026_fridays_are_working_days_and_holidays_excluded(self):
        # Nov 2026: 30 days. Nov 1 is Sunday. Suppose Nov 10 (Tue) is Diwali holiday.
        cal = build_indian_calendar_summary(
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 30),
            weekend_days_str="Saturday,Sunday",
            holidays=[(date(2026, 11, 10), "Diwali")],
        )
        assert cal.total_calendar_days == 30
        # Nov 6, 13, 20, 27 are Fridays -> MUST be working days in India (DEF-03 fixed!)
        assert date(2026, 11, 6) in cal.scheduled_working_dates
        assert date(2026, 11, 13) in cal.scheduled_working_dates
        # Saturdays/Sundays & Diwali -> non-working
        assert date(2026, 11, 1) in cal.non_working_dates
        assert date(2026, 11, 7) in cal.non_working_dates
        assert date(2026, 11, 10) in cal.non_working_dates
        assert date(2026, 11, 10) not in cal.scheduled_working_dates
        # Nov 2026 has 9 weekend days (Sat/Sun) + 1 weekday holiday = 20 working days
        assert len(cal.scheduled_working_dates) == 20

    @pytest.mark.asyncio
    async def test_fetch_indian_calendar_reads_db(self):
        conn = MockAsyncpgConn(
            {
                "from company_settings": {"weekend_days": "Saturday,Sunday"},
                "from company_statutory_details": {"is_esic_applicable": True, "esic_establishment_code": "50000123450000001"},
                "from holidays": [
                    {"date": date(2026, 8, 15), "name": "Independence Day"},
                ],
            }
        )
        cal = await fetch_indian_calendar(
            conn, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31)
        )
        assert cal.total_calendar_days == 31
        assert date(2026, 8, 15) in cal.holiday_dates


# ---------------------------------------------------------------------------
# 2. Leave Adapter Tests
# ---------------------------------------------------------------------------
class TestLeaveAdapter:
    def setup_method(self):
        self.cal = build_indian_calendar_summary(
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 30),
            holidays=[(date(2026, 11, 10), "Holiday")],
        )

    def test_paid_vs_unpaid_half_day_and_weekend_skipping(self):
        # Nov 6 (Fri) to Nov 9 (Mon) -> spans Sat/Sun (Nov 7, 8) -> only 2 working days!
        records = [
            LeaveRecord(
                leave_type="unpaid",
                start_date=date(2026, 11, 6),
                end_date=date(2026, 11, 9),
                half_day=False,
            ),
            # Half-day sick leave on Nov 12 (Thu)
            LeaveRecord(
                leave_type="sick",
                start_date=date(2026, 11, 12),
                end_date=date(2026, 11, 12),
                half_day=True,
            ),
        ]
        summary = aggregate_employee_leaves(
            employee_id="emp-1", records=records, calendar_summary=self.cal
        )
        assert summary.unpaid_leave_days == Decimal("2")
        assert summary.paid_leave_days == Decimal("0.5")
        assert date(2026, 11, 7) not in summary.unpaid_leave_weights

    def test_month_boundary_clamping(self):
        # Leave Nov 30 (Mon) through Dec 2 (Wed) -> only Nov 30 belongs to Nov cycle
        records = [
            LeaveRecord(
                leave_type="lop",
                start_date=date(2026, 11, 30),
                end_date=date(2026, 12, 2),
                half_day=False,
            )
        ]
        summary = aggregate_employee_leaves(
            employee_id="emp-1", records=records, calendar_summary=self.cal
        )
        assert summary.unpaid_leave_days == Decimal("1")

    @pytest.mark.asyncio
    async def test_fetch_leaves_batch_filters_approved_only(self):
        conn = MockAsyncpgConn(
            {
                "from leaves l": [
                    {
                        "employee_id": "emp-1",
                        "leave_type": "casual",
                        "start_date": date(2026, 11, 2),
                        "end_date": date(2026, 11, 3),
                        "half_day": False,
                    }
                ]
            }
        )
        res = await fetch_leaves_batch(
            conn, employee_ids=["emp-1"], calendar_summary=self.cal
        )
        assert res["emp-1"].paid_leave_days == Decimal("2")
        sql_executed = conn.executed_queries[0][0].lower()
        assert "l.status = 'approved'" in sql_executed


# ---------------------------------------------------------------------------
# 3. Attendance Adapter Tests
# ---------------------------------------------------------------------------
class TestAttendanceAdapter:
    def setup_method(self):
        self.cal = build_indian_calendar_summary(
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 30),
            holidays=[(date(2026, 11, 10), "Holiday")],
        )

    def test_absent_days_exclude_weekends_holidays_leaves_and_future_days(self):
        # Working days up to Friday Nov 6: Nov 2, 3, 4, 5, 6 (5 working days)
        # Paid leave on Nov 4. Present via punch/reg on Nov 2, 3, 5. Missed Nov 6 -> 1 absent!
        # Days after as_of_date=Nov 6 (Nov 9..30) must NOT be counted absent.
        leave_sum = aggregate_employee_leaves(
            employee_id="emp-1",
            records=[
                LeaveRecord(
                    leave_type="sick",
                    start_date=date(2026, 11, 4),
                    end_date=date(2026, 11, 4),
                )
            ],
            calendar_summary=self.cal,
        )
        att = compute_attendance_summary(
            employee_id="emp-1",
            calendar_summary=self.cal,
            leave_summary=leave_sum,
            present_dates={date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 5)},
            cutoff_date=date(2026, 11, 25),
            as_of_date=date(2026, 11, 6),
        )
        assert att.unexcused_absent_days == Decimal("1")
        assert att.absent_dates == [date(2026, 11, 6)]

    def test_mid_month_joiner_and_exit_non_employed_proration(self):
        # Joined Nov 16 (Mon) -> Nov 1..15 (15 days) non-employed; punched all days Nov 16..20
        empty_leave = aggregate_employee_leaves(
            employee_id="emp-join", records=[], calendar_summary=self.cal
        )
        att = compute_attendance_summary(
            employee_id="emp-join",
            calendar_summary=self.cal,
            leave_summary=empty_leave,
            present_dates={
                date(2026, 11, 16),
                date(2026, 11, 17),
                date(2026, 11, 18),
                date(2026, 11, 19),
                date(2026, 11, 20),
            },
            cutoff_date=date(2026, 11, 25),
            joining_date=date(2026, 11, 16),
            as_of_date=date(2026, 11, 20),
        )
        assert att.non_employed_days == Decimal("15")
        assert att.unpaid_leave_days == Decimal("15")
        assert att.unexcused_absent_days == Decimal("0")

    def test_strict_attendance_false_preserves_zero_unexcused(self):
        empty_leave = aggregate_employee_leaves(
            employee_id="emp-field", records=[], calendar_summary=self.cal
        )
        att = compute_attendance_summary(
            employee_id="emp-field",
            calendar_summary=self.cal,
            leave_summary=empty_leave,
            present_dates=set(),
            cutoff_date=date(2026, 11, 5),
            as_of_date=date(2026, 11, 5),
            strict_attendance=False,
        )
        assert att.raw_missing_punch_days == Decimal("4")  # Nov 2,3,4,5
        assert att.unexcused_absent_days == Decimal("0")

    @pytest.mark.asyncio
    async def test_fetch_attendance_batch_uses_ist_timezone_and_regularisations(self):
        conn = MockAsyncpgConn(
            {
                "from attendance a": [],
                "from attendance ": [
                    {"employee_id": "emp-1", "work_date": date(2026, 11, 2)},
                    {"employee_id": "emp-1", "work_date": date(2026, 11, 3)},
                ],
                "from attendance_regularisations": [
                    {"employee_id": "emp-1", "work_date": date(2026, 11, 4)},
                ],
            }
        )
        leave_map = {
            "emp-1": aggregate_employee_leaves(
                employee_id="emp-1", records=[], calendar_summary=self.cal
            )
        }
        res = await fetch_attendance_batch(
            conn,
            employee_meta=[
                {"employee_id": "emp-1", "joining_date": date(2026, 1, 1), "date_of_exit": None}
            ],
            calendar_summary=self.cal,
            leaves_by_emp=leave_map,
            cutoff_date=date(2026, 11, 4),
            as_of_date=date(2026, 11, 4),
        )
        assert res["emp-1"].unexcused_absent_days == Decimal("0")
        assert "AT TIME ZONE 'Asia/Kolkata'" in conn.executed_queries[0][0]


# ---------------------------------------------------------------------------
# 4. Expense & Advance Adapter Tests
# ---------------------------------------------------------------------------
class TestExpenseAndAdvanceAdapters:
    @pytest.mark.asyncio
    async def test_expense_adapter_filters_approved_unpaid_before_cutoff(self):
        conn = MockAsyncpgConn(
            {
                "from reimbursements": [
                    {
                        "employee_id": "emp-1",
                        "reimbursement_id": "claim-1",
                        "category": "Travel",
                        "subcategory": "Cab",
                        "amount": Decimal("1250.50"),
                        "expense_date": date(2026, 11, 12),
                        "decided_at": datetime(2026, 11, 13, 10, 0, tzinfo=timezone.utc),
                    },
                    {
                        "employee_id": "emp-1",
                        "reimbursement_id": "claim-2",
                        "category": "Internet",
                        "subcategory": None,
                        "amount": Decimal("749.50"),
                        "expense_date": date(2026, 11, 18),
                        "decided_at": None,
                    },
                ]
            }
        )
        res = await fetch_reimbursements_batch(
            conn, employee_ids=["emp-1"], cutoff_date=date(2026, 11, 25)
        )
        assert res["emp-1"].total_approved_reimbursements == Decimal("2000.00")
        assert res["emp-1"].reimbursement_ids == ["claim-1", "claim-2"]
        sql = " ".join(conn.executed_queries[0][0].split()).lower()
        assert "status = 'approved'" in sql
        assert "paid_at is null" in sql
        assert "expense_date <= $2" in sql

    def test_advance_adapter_caps_final_installment_to_remaining_balance(self):
        summary = compute_advance_summary(
            employee_id="emp-1",
            loans=[
                AdvanceLoanRow(
                    advance_id="adv-1",
                    principal_amount=Decimal("20000"),
                    monthly_emi_amount=Decimal("5000"),
                    total_installments=4,
                    paid_installments=3,
                    remaining_balance=Decimal("1800"),  # Final partial balance < EMI!
                ),
                AdvanceLoanRow(
                    advance_id="adv-done",
                    principal_amount=Decimal("10000"),
                    monthly_emi_amount=Decimal("2500"),
                    total_installments=4,
                    paid_installments=4,
                    remaining_balance=Decimal("0"),
                ),
            ],
        )
        assert summary.total_scheduled_emi == Decimal("1800.00")
        assert len(summary.advance_recoveries) == 1
        assert summary.advance_recoveries[0].advance_id == "adv-1"

    @pytest.mark.asyncio
    async def test_fetch_advances_batch_sql(self):
        conn = MockAsyncpgConn(
            {
                "from employee_salary_advances": [
                    {
                        "employee_id": "emp-1",
                        "advance_id": "adv-1",
                        "principal_amount": Decimal("10000"),
                        "monthly_emi_amount": Decimal("2500"),
                        "total_installments": 4,
                        "paid_installments": 1,
                        "remaining_balance": Decimal("7500"),
                    }
                ]
            }
        )
        res = await fetch_advances_batch(conn, employee_ids=["emp-1"])
        assert res["emp-1"].total_scheduled_emi == Decimal("2500.00")


# ---------------------------------------------------------------------------
# 5. Aggregator, End-to-End Pipeline & Read-Only SQL Security Audit
# ---------------------------------------------------------------------------
class TestAggregatorEndToEndAndReadOnlyAudit:
    def test_financial_year_label_mapping(self):
        assert get_financial_year_label(4, 2026) == ("2026-2027", 2026, 2027)
        assert get_financial_year_label(11, 2026) == ("2026-2027", 2026, 2027)
        assert get_financial_year_label(1, 2027) == ("2026-2027", 2026, 2027)
        assert get_financial_year_label(3, 2027) == ("2026-2027", 2026, 2027)

    def test_select_latest_pt_slabs_deduplicates_duplicate_seeds(self):
        rows = [
            {
                "state_code": "KA",
                "state_name": "Karnataka",
                "effective_from": date(2024, 4, 1),
                "min_monthly_gross": Decimal("0"),
                "max_monthly_gross": Decimal("15000"),
                "pt_amount": Decimal("0"),
                "special_feb_amount": None,
            },
            # Simulate duplicate row from repeated script run
            {
                "state_code": "KA",
                "state_name": "Karnataka",
                "effective_from": date(2024, 4, 1),
                "min_monthly_gross": Decimal("0"),
                "max_monthly_gross": Decimal("15000"),
                "pt_amount": Decimal("0"),
                "special_feb_amount": None,
            },
            {
                "state_code": "KA",
                "state_name": "Karnataka",
                "effective_from": date(2024, 4, 1),
                "min_monthly_gross": Decimal("15000.01"),
                "max_monthly_gross": None,
                "pt_amount": Decimal("200"),
                "special_feb_amount": None,
            },
        ]
        slabs = select_latest_pt_slabs(rows)
        assert len(slabs) == 2

    @pytest.mark.asyncio
    async def test_full_ingestion_to_engine_pipeline_and_readonly_audit(self):
        """
        Simulate complete Phase 3 ingestion -> Phase 2 engine execution for Darryl
        with 0 LOP, Rs. 3,500 approved reimbursement, and Rs. 2,000 advance EMI.
        """
        darryl_id = "c0ede84e-56f9-48f8-a133-0cb68d618be7"
        cal = build_indian_calendar_summary(
            start_date=date(2026, 11, 1), end_date=date(2026, 11, 30)
        )
        darryl_punches = [
            {"employee_id": darryl_id, "work_date": d}
            for d in cal.scheduled_working_dates
            if d <= date(2026, 11, 25)
        ]

        conn = MockAsyncpgConn(
            {
                "from employee_salary_structures ess": [
                    {
                        "employee_id": darryl_id,
                        "employee_name": "Darryl M A",
                        "email": "darryl@aimploy.com",
                        "joining_date": date(2024, 1, 1),
                        "date_of_exit": None,
                        "pan_number": "ABCDE1234F",
                        "uan_number": "101234567890",
                        "esic_ip_number": None,
                        "monthly_basic_pay": Decimal("50000"),
                        "monthly_hra": Decimal("25000"),
                        "monthly_special_allowance": Decimal("25000"),
                        "monthly_da": Decimal("0"),
                        "is_epf_eligible": True,
                        "epf_wage_cap_applied": True,
                        "vpf_percentage": Decimal("0"),
                        "is_esi_eligible": False,
                        "pt_state_code": "KA",
                        "tax_regime": "NEW",
                    }
                ],
                "from company_settings": {"weekend_days": "Saturday,Sunday"},
                "from company_statutory_details": {"is_esic_applicable": True, "esic_establishment_code": "50000123450000001"},
                "from holidays": [],
                "from pt_state_slabs": [
                    {
                        "state_code": "KA",
                        "state_name": "Karnataka",
                        "effective_from": date(2024, 4, 1),
                        "min_monthly_gross": Decimal("0"),
                        "max_monthly_gross": Decimal("15000"),
                        "pt_amount": Decimal("0"),
                        "special_feb_amount": None,
                    },
                    {
                        "state_code": "KA",
                        "state_name": "Karnataka",
                        "effective_from": date(2024, 4, 1),
                        "min_monthly_gross": Decimal("15000.01"),
                        "max_monthly_gross": None,
                        "pt_amount": Decimal("200"),
                        "special_feb_amount": None,
                    },
                ],
                "from employee_tax_declarations": [],
                "from employee_bank_details": [
                    {
                        "employee_id": darryl_id,
                        "payment_method": "BANK_TRANSFER",
                        "account_number": "50100492817291",
                        "ifsc_code": "HDFC0000240",
                    }
                ],
                "from payroll_entries pe": [],
                "from leaves l": [],
                "from attendance a": darryl_punches,
                "from attendance ": darryl_punches,
                "from attendance_regularisations": [],
                "from reimbursements": [
                    {
                        "employee_id": darryl_id,
                        "reimbursement_id": "reim-101",
                        "category": "Travel",
                        "subcategory": "Flight",
                        "amount": Decimal("3500.00"),
                        "expense_date": date(2026, 11, 10),
                        "decided_at": None,
                    }
                ],
                "from employee_salary_advances": [
                    {
                        "employee_id": darryl_id,
                        "advance_id": "adv-101",
                        "principal_amount": Decimal("10000"),
                        "monthly_emi_amount": Decimal("2000"),
                        "total_installments": 5,
                        "paid_installments": 0,
                        "remaining_balance": Decimal("10000"),
                    }
                ],
            }
        )

        ingestion = await fetch_cycle_employee_inputs(
            conn,
            cycle_month=11,
            cycle_year=2026,
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 30),
            cutoff_date=date(2026, 11, 25),
            as_of_date=date(2026, 11, 25),
        )

        assert len(ingestion.bundles) == 1
        bundle = ingestion.bundles[0]
        assert bundle.missing_statutory_warnings == []
        assert bundle.reimbursement_ids == ["reim-101"]

        # Execute Phase 2 Pure Engine directly on ingested bundle!
        calc = calculate_monthly_payroll(bundle.engine_input)
        assert calc.gross_earnings == Decimal("100000.00")
        assert calc.reimbursements_amount == Decimal("3500.00")
        assert calc.advance_recovery_amount == Decimal("2000.00")
        # Baseline Rs. 92,042 + Rs. 3,500 reimbursement - Rs. 2,000 advance EMI = Rs. 93,542.00
        assert calc.net_payable == Decimal("93542.00")

        # Audit ALL executed SQL queries: strictly read-only SELECT!
        forbidden_dml = re.compile(r"\b(insert|update|delete|drop|alter|truncate|create)\b", re.I)
        for sql, _args in conn.executed_queries:
            assert sql.strip().upper().startswith("SELECT"), f"Non-SELECT query: {sql}"
            assert not forbidden_dml.search(sql), f"Forbidden DML/DDL keyword in: {sql}"

    @pytest.mark.asyncio
    async def test_off_cycle_sql_filters_held_entries_and_surfaces_missing_kyc_warnings(self):
        emp_id = "emp-held-1"
        conn = MockAsyncpgConn(
            {
                "from employee_salary_structures ess": [
                    {
                        "employee_id": emp_id,
                        "employee_name": "Held Employee",
                        "email": "held@aimploy.com",
                        "joining_date": date(2025, 1, 1),
                        "date_of_exit": None,
                        "pan_number": None,
                        "uan_number": None,
                        "esic_ip_number": None,
                        "monthly_basic_pay": Decimal("20000"),
                        "monthly_hra": Decimal("10000"),
                        "monthly_special_allowance": Decimal("10000"),
                        "monthly_da": Decimal("0"),
                        "is_epf_eligible": True,
                        "epf_wage_cap_applied": True,
                        "vpf_percentage": Decimal("0"),
                        "is_esi_eligible": False,
                        "pt_state_code": "KA",
                        "tax_regime": "NEW",
                    }
                ],
                "from company_settings": None,
                "from company_statutory_details": {"is_esic_applicable": True, "esic_establishment_code": "50000123450000001"},
                "from holidays": [],
                "from pt_state_slabs": [],
                "from employee_tax_declarations": [],
                "from employee_bank_details": [],
                "from payroll_entries pe": [],
                "from leaves l": [],
                "from attendance ": [],
                "from attendance_regularisations": [],
                "from reimbursements": [],
                "from employee_salary_advances": [],
            }
        )
        ingestion = await fetch_cycle_employee_inputs(
            conn,
            cycle_month=11,
            cycle_year=2026,
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 30),
            cutoff_date=date(2026, 11, 25),
            reference_cycle_id="cycle-orig-uuid",
            strict_attendance=False,
        )
        emp_query = conn.executed_queries[0][0]
        assert "pe.status = 'ON_HOLD'" in emp_query
        assert "pe2.payout_status = 'PAID'" in emp_query

        warnings = ingestion.bundles[0].missing_statutory_warnings
        assert any("PAN" in w for w in warnings)
        assert any("UAN" in w for w in warnings)
        assert any("bank account" in w for w in warnings)
