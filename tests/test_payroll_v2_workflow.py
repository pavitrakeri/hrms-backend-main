"""
Offline End-to-End API & State-Machine Tests for Phase 4 Payroll V2.

Runs 100% locally in memory using a stateful fake asyncpg pool + FastAPI TestClient.
ZERO network calls and ZERO live calls to Neon Cloud PostgreSQL.

Covers:
  1. Pydantic v2 DTO validations (IFSC, PAN, TAN, UAN, dates, rejection comments, hold reasons)
  2. Master setup APIs (Company Statutory, PT Slabs, Salary Structures, Bank Details, Advances, Tax Decl)
  3. Duplicate REGULAR cycle prevention (HTTP 409) & OFF_CYCLE reference linking
  4. Full batch calculation + Line-Item Hold dynamic aggregate recalculation (CLEARED only)
  5. Sequential 3-tier approval chain (HR -> Finance -> CFO) & out-of-order 403 enforcement
  6. Rejection & recalculation reset flow (preserving manual holds)
  7. Final CFO disbursement settlement:
       - Only CLEARED entries become PAID (ON_HOLD entries stay PENDING)
       - Only CLEARED employees' approved reimbursements become paid
       - Only CLEARED employees' active salary advances reduce balance & mark COMPLETED
  8. Off-Cycle recovery run targeting held employees from a disbursed Regular cycle
  9. Employee Self-Service (/me/payslips, /me/payslips/{id} strict isolation, /me/provisional-preview, /me/tax-declarations)
  10. RBAC 403 matrix across employee, hr, finance, cfo, and admin roles
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from app.datamodels.payroll_v2_models import (
    BankDetailsUpsertRequest,
    CycleApprovalActionRequest,
    CycleCreateRequest,
    EmployeeStatutoryProfileUpdate,
    EntryHoldRequest,
    SalaryAdvanceCreateRequest,
    SalaryStructureUpsertRequest,
)
from app.features.auth import get_current_user
from app.main import app


# ---------------------------------------------------------------------------
# Stateful In-Memory PostgreSQL Emulator for Payroll V2
# ---------------------------------------------------------------------------
class StatefulPayrollDB:
    """In-memory relational state simulating the 10 Payroll V2 + upstream tables."""

    def __init__(self) -> None:
        now = datetime.now(timezone.utc)
        self.company_id = str(uuid.uuid4())
        self.hr_id = str(uuid.uuid4())
        self.finance_id = str(uuid.uuid4())
        self.cfo_id = str(uuid.uuid4())
        self.emp_darryl_id = str(uuid.uuid4())
        self.emp_ajmal_id = str(uuid.uuid4())
        self.emp_damewan_id = str(uuid.uuid4())

        self.company_statutory: list[dict[str, Any]] = [
            {
                "id": self.company_id,
                "company_name": "Aimploy Technologies India Private Limited",
                "tan_number": "BLRA12345C",
                "pan_number": "AABCA1234F",
                "epfo_establishment_code": "BGBNG0012345000",
                "esic_establishment_code": "50000123450000001",
                "is_esic_applicable": True,
                "pt_rc_number": "KA-PTRC-987654",
                "pt_ec_number": "KA-PTEC-123456",
                "registered_address": "Bengaluru, Karnataka 560103",
                "created_at": now,
                "updated_at": now,
            }
        ]

        self.departments: dict[str, str] = {
            "dept-eng": "Engineering",
            "dept-ops": "Operations",
        }

        self.users: dict[str, dict[str, Any]] = {
            self.hr_id: {
                "id": self.hr_id,
                "full_name": "Priya Nair",
                "email": "hr@aimploy.com",
                "department_id": "dept-ops",
                "designation": "HR Manager",
                "joining_date": date(2024, 1, 1),
                "date_of_exit": None,
                "exit_reason_code": None,
                "pan_number": "ABCDE1111F",
                "uan_number": "100111111111",
                "aadhaar_number": None,
                "esic_ip_number": None,
                "father_or_spouse_name": None,
                "is_active": True,
            },
            self.finance_id: {
                "id": self.finance_id,
                "full_name": "Rohan Kulkarni",
                "email": "finance@aimploy.com",
                "department_id": "dept-ops",
                "designation": "Finance Manager",
                "joining_date": date(2024, 1, 1),
                "date_of_exit": None,
                "exit_reason_code": None,
                "pan_number": "ABCDE2222F",
                "uan_number": "100222222222",
                "aadhaar_number": None,
                "esic_ip_number": None,
                "father_or_spouse_name": None,
                "is_active": True,
            },
            self.cfo_id: {
                "id": self.cfo_id,
                "full_name": "Vikram Mehta",
                "email": "cfo@aimploy.com",
                "department_id": "dept-ops",
                "designation": "Chief Financial Officer",
                "joining_date": date(2024, 1, 1),
                "date_of_exit": None,
                "exit_reason_code": None,
                "pan_number": "ABCDE3333F",
                "uan_number": "100333333333",
                "aadhaar_number": None,
                "esic_ip_number": None,
                "father_or_spouse_name": None,
                "is_active": True,
            },
            self.emp_darryl_id: {
                "id": self.emp_darryl_id,
                "full_name": "Darryl M A",
                "email": "darryl@aimploy.com",
                "department_id": "dept-eng",
                "designation": "Senior Software Engineer",
                "joining_date": date(2024, 4, 1),
                "date_of_exit": None,
                "exit_reason_code": None,
                "pan_number": "BGLPD1234K",
                "uan_number": "101234567890",
                "aadhaar_number": None,
                "esic_ip_number": None,
                "father_or_spouse_name": "Anthony M",
                "is_active": True,
            },
            self.emp_ajmal_id: {
                "id": self.emp_ajmal_id,
                "full_name": "Ajmal Asharudeen",
                "email": "ajmal@aimploy.com",
                "department_id": "dept-eng",
                "designation": "Product Engineer",
                "joining_date": date(2024, 6, 1),
                "date_of_exit": None,
                "exit_reason_code": None,
                "pan_number": "BGLPA5678M",
                "uan_number": "109876543210",
                "aadhaar_number": None,
                "esic_ip_number": None,
                "father_or_spouse_name": "Asharudeen K",
                "is_active": True,
            },
            self.emp_damewan_id: {
                "id": self.emp_damewan_id,
                "full_name": "Damewan Bareh",
                "email": "damewan@aimploy.com",
                "department_id": "dept-ops",
                "designation": "Operations Associate",
                "joining_date": date(2025, 1, 1),
                "date_of_exit": None,
                "exit_reason_code": None,
                "pan_number": "BGLPB9012N",
                "uan_number": "105555555555",
                "aadhaar_number": None,
                "esic_ip_number": "3112345678",
                "father_or_spouse_name": "Kyrshan Bareh",
                "is_active": True,
            },
        }

        self.salary_structures: list[dict[str, Any]] = [
            {
                "id": str(uuid.uuid4()),
                "employee_id": self.emp_darryl_id,
                "effective_from": date(2026, 4, 1),
                "effective_to": None,
                "is_active": True,
                "base_currency": "INR",
                "annual_ctc": Decimal("1200000.00"),
                "monthly_basic_pay": Decimal("50000.00"),
                "monthly_hra": Decimal("20000.00"),
                "monthly_special_allowance": Decimal("30000.00"),
                "monthly_da": Decimal("0.00"),
                "is_epf_eligible": True,
                "epf_wage_cap_applied": True,
                "vpf_percentage": Decimal("0.00"),
                "is_esi_eligible": False,
                "pt_state_code": "KA",
                "tax_regime": "NEW",
                "created_at": now,
                "updated_at": now,
            },
            {
                "id": str(uuid.uuid4()),
                "employee_id": self.emp_ajmal_id,
                "effective_from": date(2026, 4, 1),
                "effective_to": None,
                "is_active": True,
                "base_currency": "INR",
                "annual_ctc": Decimal("480000.00"),
                "monthly_basic_pay": Decimal("20000.00"),
                "monthly_hra": Decimal("8000.00"),
                "monthly_special_allowance": Decimal("12000.00"),
                "monthly_da": Decimal("0.00"),
                "is_epf_eligible": True,
                "epf_wage_cap_applied": True,
                "vpf_percentage": Decimal("0.00"),
                "is_esi_eligible": False,
                "pt_state_code": "KA",
                "tax_regime": "NEW",
                "created_at": now,
                "updated_at": now,
            },
            {
                "id": str(uuid.uuid4()),
                "employee_id": self.emp_damewan_id,
                "effective_from": date(2026, 4, 1),
                "effective_to": None,
                "is_active": True,
                "base_currency": "INR",
                "annual_ctc": Decimal("240000.00"),
                "monthly_basic_pay": Decimal("10000.00"),
                "monthly_hra": Decimal("4000.00"),
                "monthly_special_allowance": Decimal("4000.00"),
                "monthly_da": Decimal("0.00"),
                "is_epf_eligible": True,
                "epf_wage_cap_applied": True,
                "vpf_percentage": Decimal("0.00"),
                "is_esi_eligible": True,
                "pt_state_code": "KA",
                "tax_regime": "NEW",
                "created_at": now,
                "updated_at": now,
            },
        ]

        self.bank_details: list[dict[str, Any]] = [
            {
                "id": str(uuid.uuid4()),
                "employee_id": eid,
                "payment_method": "BANK_TRANSFER",
                "account_holder_name": u["full_name"],
                "bank_name": "HDFC Bank",
                "account_number": f"501000{idx}98765",
                "ifsc_code": "HDFC0000240",
                "is_primary": True,
                "created_at": now,
                "updated_at": now,
            }
            for idx, (eid, u) in enumerate(self.users.items(), start=1)
        ]

        self.tax_declarations: list[dict[str, Any]] = []

        # Reimbursements: Darryl has Rs 1,200 approved; Ajmal has Rs 2,500 approved
        self.reim_darryl_id = str(uuid.uuid4())
        self.reim_ajmal_id = str(uuid.uuid4())
        self.reimbursements: dict[str, dict[str, Any]] = {
            self.reim_darryl_id: {
                "id": self.reim_darryl_id,
                "user_id": self.emp_darryl_id,
                "category": "Travel",
                "subcategory": "Client Cab",
                "amount": Decimal("1200.00"),
                "expense_date": date(2026, 11, 12),
                "status": "approved",
                "paid_at": None,
            },
            self.reim_ajmal_id: {
                "id": self.reim_ajmal_id,
                "user_id": self.emp_ajmal_id,
                "category": "Equipment",
                "subcategory": "Monitor Stand",
                "amount": Decimal("2500.00"),
                "expense_date": date(2026, 11, 15),
                "status": "approved",
                "paid_at": None,
            },
        }

        # Advances: Damewan has 1 final EMI of Rs 1,000 left; Ajmal has Rs 5,000 EMI on Rs 15,000 loan
        self.adv_damewan_id = str(uuid.uuid4())
        self.adv_ajmal_id = str(uuid.uuid4())
        self.advances: dict[str, dict[str, Any]] = {
            self.adv_damewan_id: {
                "id": self.adv_damewan_id,
                "employee_id": self.emp_damewan_id,
                "principal_amount": Decimal("3000.00"),
                "monthly_emi_amount": Decimal("1000.00"),
                "total_installments": 3,
                "paid_installments": 2,
                "remaining_balance": Decimal("1000.00"),
                "status": "ACTIVE",
                "created_at": now,
                "updated_at": now,
            },
            self.adv_ajmal_id: {
                "id": self.adv_ajmal_id,
                "employee_id": self.emp_ajmal_id,
                "principal_amount": Decimal("15000.00"),
                "monthly_emi_amount": Decimal("5000.00"),
                "total_installments": 3,
                "paid_installments": 0,
                "remaining_balance": Decimal("15000.00"),
                "status": "ACTIVE",
                "created_at": now,
                "updated_at": now,
            },
        }

        # Damewan has 2 days approved unpaid leave in Nov 2026 (Nov 18 & Nov 19)
        self.leaves: list[dict[str, Any]] = [
            {
                "id": str(uuid.uuid4()),
                "user_id": self.emp_damewan_id,
                "leave_type": "lop",
                "start_date": date(2026, 11, 18),
                "end_date": date(2026, 11, 19),
                "half_day": False,
                "status": "approved",
            }
        ]

        # Populate full weekday clock-ins across Nov & Dec 2026 for active staff
        # (skipping Damewan's 2 approved LOP days so ncp_days = 2.0 exactly)
        self.attendance: list[dict[str, Any]] = []
        d_cur = date(2026, 11, 1)
        while d_cur <= date(2026, 12, 31):
            if d_cur.weekday() not in (5, 6):
                for eid in (self.emp_darryl_id, self.emp_ajmal_id, self.emp_damewan_id):
                    if eid == self.emp_damewan_id and d_cur in (date(2026, 11, 18), date(2026, 11, 19)):
                        continue
                    self.attendance.append({"employee_id": eid, "work_date": d_cur})
            d_cur += timedelta(days=1)

        self.cycles: dict[str, dict[str, Any]] = {}
        self.entries: dict[str, dict[str, Any]] = {}
        self.approvals: list[dict[str, Any]] = []


class FakeAsyncpgConn:
    """Executes SQL queries against StatefulPayrollDB."""

    def __init__(self, db: StatefulPayrollDB) -> None:
        self.db = db

    @asynccontextmanager
    async def transaction(self):
        yield self

    def _norm(self, sql: str) -> str:
        return " ".join(sql.split()).lower()

    async def fetchval(self, sql: str, *args: Any) -> Any:
        q = self._norm(sql)
        if "select 1 from payroll_cycles where id =" in q:
            return 1 if str(args[0]) in self.db.cycles else None
        if "select 1 from payroll_cycles where cycle_year =" in q:
            yr, mo = int(args[0]), int(args[1])
            for c in self.db.cycles.values():
                if c["cycle_year"] == yr and c["cycle_month"] == mo and c["cycle_type"] == "REGULAR":
                    return 1
            return None
        if "select 1 from users where id =" in q:
            return 1 if str(args[0]) in self.db.users else None
        raise AssertionError(f"Unhandled fetchval SQL: {q}")

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        q = self._norm(sql)
        now = datetime.now(timezone.utc)

        # Company statutory
        if "from company_statutory_details" in q:
            if not self.db.company_statutory:
                return None
            return dict(self.db.company_statutory[-1])
        if q.startswith("update company_statutory_details"):
            cid = str(args[0])
            for c in self.db.company_statutory:
                if c["id"] == cid:
                    c.update(
                        company_name=args[1],
                        tan_number=args[2],
                        pan_number=args[3],
                        epfo_establishment_code=args[4],
                        esic_establishment_code=args[5],
                        is_esic_applicable=args[6],
                        pt_rc_number=args[7],
                        pt_ec_number=args[8],
                        registered_address=args[9],
                        updated_at=now,
                    )
                    return dict(c)
            return None
        if q.startswith("insert into company_statutory_details"):
            row = {
                "id": str(uuid.uuid4()),
                "company_name": args[0],
                "tan_number": args[1],
                "pan_number": args[2],
                "epfo_establishment_code": args[3],
                "esic_establishment_code": args[4],
                "is_esic_applicable": args[5],
                "pt_rc_number": args[6],
                "pt_ec_number": args[7],
                "registered_address": args[8],
                "created_at": now,
                "updated_at": now,
            }
            self.db.company_statutory.append(row)
            return dict(row)

        # Calendar service company_settings
        if "from company_settings" in q:
            return {"weekend_days": "Saturday,Sunday"}

        # Users checks & statutory profile update
        if q.startswith("select id, full_name, email from users"):
            u = self.db.users.get(str(args[0]))
            return dict(u) if u and u["is_active"] else None
        if q.startswith("select id, full_name from users"):
            u = self.db.users.get(str(args[0]))
            return dict(u) if u and u["is_active"] else None
        if q.startswith("update users set pan_number ="):
            u = self.db.users.get(str(args[0]))
            if not u:
                return None
            if args[1] is not None:
                u["pan_number"] = args[1]
            if args[2] is not None:
                u["uan_number"] = args[2]
            if args[3] is not None:
                u["aadhaar_number"] = args[3]
            if args[4] is not None:
                u["esic_ip_number"] = args[4]
            if args[5] is not None:
                u["father_or_spouse_name"] = args[5]
            u["date_of_exit"] = args[6]
            u["exit_reason_code"] = args[7]
            return {
                "employee_id": u["id"],
                "full_name": u["full_name"],
                "pan_number": u["pan_number"],
                "uan_number": u["uan_number"],
                "aadhaar_number": u["aadhaar_number"],
                "esic_ip_number": u["esic_ip_number"],
                "father_or_spouse_name": u["father_or_spouse_name"],
                "date_of_exit": u["date_of_exit"],
                "exit_reason_code": u["exit_reason_code"],
            }

        # Salary structures insert
        if q.startswith("insert into employee_salary_structures"):
            row = {
                "id": str(uuid.uuid4()),
                "employee_id": str(args[0]),
                "effective_from": args[1],
                "effective_to": None,
                "is_active": True,
                "base_currency": args[2],
                "annual_ctc": args[3],
                "monthly_basic_pay": args[4],
                "monthly_hra": args[5],
                "monthly_special_allowance": args[6],
                "monthly_da": args[7],
                "is_epf_eligible": args[8],
                "epf_wage_cap_applied": args[9],
                "vpf_percentage": args[10],
                "is_esi_eligible": args[11],
                "pt_state_code": args[12],
                "tax_regime": args[13],
                "created_at": now,
                "updated_at": now,
            }
            self.db.salary_structures.append(row)
            return dict(row)

        # Bank details
        if "from employee_bank_details where employee_id =" in q:
            eid = str(args[0])
            matches = [b for b in self.db.bank_details if b["employee_id"] == eid]
            if not matches:
                return None
            matches.sort(key=lambda x: (x["is_primary"], x["updated_at"]), reverse=True)
            return dict(matches[0])
        if q.startswith("insert into employee_bank_details"):
            row = {
                "id": str(uuid.uuid4()),
                "employee_id": str(args[0]),
                "payment_method": args[1],
                "account_holder_name": args[2],
                "bank_name": args[3],
                "account_number": args[4],
                "ifsc_code": args[5],
                "is_primary": args[6],
                "created_at": now,
                "updated_at": now,
            }
            self.db.bank_details.append(row)
            return dict(row)

        # Tax declarations
        if "from employee_tax_declarations etd" in q and "where etd.employee_id =" in q:
            eid, fy = str(args[0]), args[1]
            for d in self.db.tax_declarations:
                if d["employee_id"] == eid and d["financial_year"] == fy:
                    u = self.db.users.get(eid, {})
                    out = dict(d)
                    out["employee_name"] = u.get("full_name")
                    return out
            return None
        if q.startswith("insert into employee_tax_declarations"):
            eid, fy = str(args[0]), args[1]
            existing = next(
                (d for d in self.db.tax_declarations if d["employee_id"] == eid and d["financial_year"] == fy),
                None,
            )
            if existing:
                existing.update(
                    regime=args[2],
                    section_80c_amount=args[3],
                    section_80d_amount=args[4],
                    annual_rent_paid=args[5],
                    home_loan_interest=args[6],
                    nps_80ccd_1b=args[7],
                    is_verified=False,
                    updated_at=now,
                )
                return dict(existing)
            row = {
                "id": str(uuid.uuid4()),
                "employee_id": eid,
                "financial_year": fy,
                "regime": args[2],
                "section_80c_amount": args[3],
                "section_80d_amount": args[4],
                "annual_rent_paid": args[5],
                "home_loan_interest": args[6],
                "nps_80ccd_1b": args[7],
                "is_verified": False,
                "created_at": now,
                "updated_at": now,
            }
            self.db.tax_declarations.append(row)
            return dict(row)
        if q.startswith("update employee_tax_declarations set is_verified ="):
            did = str(args[0])
            for d in self.db.tax_declarations:
                if d["id"] == did:
                    d["is_verified"] = bool(args[1])
                    d["updated_at"] = now
                    return dict(d)
            return None

        # Advances
        if "select status from employee_salary_advances where id =" in q:
            adv = self.db.advances.get(str(args[0]))
            return {"status": adv["status"]} if adv else None
        if q.startswith("insert into employee_salary_advances"):
            row = {
                "id": str(uuid.uuid4()),
                "employee_id": str(args[0]),
                "principal_amount": args[1],
                "monthly_emi_amount": args[2],
                "total_installments": args[3],
                "paid_installments": 0,
                "remaining_balance": args[1],
                "status": "ACTIVE",
                "created_at": now,
                "updated_at": now,
            }
            self.db.advances[row["id"]] = row
            return dict(row)
        if q.startswith("update employee_salary_advances set status ="):
            adv = self.db.advances.get(str(args[0]))
            if not adv:
                return None
            adv["status"] = args[1]
            adv["updated_at"] = now
            return dict(adv)

        # Payroll cycles
        if q.startswith("insert into payroll_cycles"):
            if args[6] == "REGULAR":
                for c in self.db.cycles.values():
                    if (
                        c["cycle_year"] == args[2]
                        and c["cycle_month"] == args[1]
                        and c["cycle_type"] == "REGULAR"
                    ):
                        raise Exception("duplicate key value violates unique constraint idx_unique_regular_cycle (23505)")
            cid = str(uuid.uuid4())
            row = {
                "id": cid,
                "cycle_name": args[0],
                "cycle_month": args[1],
                "cycle_year": args[2],
                "start_date": args[3],
                "end_date": args[4],
                "cutoff_date": args[5],
                "cycle_type": args[6],
                "reference_cycle_id": str(args[7]) if args[7] else None,
                "status": "DRAFT",
                "total_gross": Decimal("0.00"),
                "total_epf_employee": Decimal("0.00"),
                "total_eps_employer": Decimal("0.00"),
                "total_epf_employer": Decimal("0.00"),
                "total_esi_employee": Decimal("0.00"),
                "total_esi_employer": Decimal("0.00"),
                "total_pt": Decimal("0.00"),
                "total_tds": Decimal("0.00"),
                "total_reimbursements": Decimal("0.00"),
                "total_advance_deductions": Decimal("0.00"),
                "total_net_payable": Decimal("0.00"),
                "total_company_cost": Decimal("0.00"),
                "employee_count": 0,
                "bank_batch_reference": None,
                "bank_export_url": None,
                "ecr_export_url": None,
                "created_by": str(args[8]),
                "approved_by": None,
                "disbursed_at": None,
                "created_at": now,
                "updated_at": now,
            }
            self.db.cycles[cid] = row
            return dict(row)

        if "from payroll_cycles where id =" in q:
            c = self.db.cycles.get(str(args[0]))
            return dict(c) if c else None

        if "from payroll_entries where cycle_id =" in q and "filter (where status = 'cleared')" in q:
            cid = str(args[0])
            entries = [e for e in self.db.entries.values() if e["cycle_id"] == cid]
            cleared = [e for e in entries if e["status"] == "CLEARED"]
            held = [e for e in entries if e["status"] == "ON_HOLD"]
            return {
                "total_gross": sum((Decimal(str(e["gross_earnings"])) for e in cleared), Decimal("0.00")),
                "total_epf_employee": sum((Decimal(str(e["epf_employee_share"])) for e in cleared), Decimal("0.00")),
                "total_eps_employer": sum((Decimal(str(e["eps_employer_share"])) for e in cleared), Decimal("0.00")),
                "total_epf_employer": sum((Decimal(str(e["epf_employer_share"])) for e in cleared), Decimal("0.00")),
                "total_esi_employee": sum((Decimal(str(e["esi_employee_share"])) for e in cleared), Decimal("0.00")),
                "total_esi_employer": sum((Decimal(str(e["esi_employer_share"])) for e in cleared), Decimal("0.00")),
                "total_pt": sum((Decimal(str(e["professional_tax"])) for e in cleared), Decimal("0.00")),
                "total_tds": sum((Decimal(str(e["tds_amount"])) for e in cleared), Decimal("0.00")),
                "total_reimbursements": sum((Decimal(str(e["reimbursements_amount"])) for e in cleared), Decimal("0.00")),
                "total_advance_deductions": sum((Decimal(str(e["advance_recovery_amount"])) for e in cleared), Decimal("0.00")),
                "total_net_payable": sum((Decimal(str(e["net_payable"])) for e in cleared), Decimal("0.00")),
                "total_company_cost": sum((Decimal(str(e["total_employer_cost"])) for e in cleared), Decimal("0.00")),
                "employee_count": len(cleared),
                "on_hold_count": len(held),
            }

        if q.startswith("update payroll_cycles set total_gross ="):
            cid = str(args[0])
            c = self.db.cycles.get(cid)
            if not c:
                return None
            c.update(
                total_gross=args[1],
                total_epf_employee=args[2],
                total_eps_employer=args[3],
                total_epf_employer=args[4],
                total_esi_employee=args[5],
                total_esi_employer=args[6],
                total_pt=args[7],
                total_tds=args[8],
                total_reimbursements=args[9],
                total_advance_deductions=args[10],
                total_net_payable=args[11],
                total_company_cost=args[12],
                employee_count=args[13],
                updated_at=now,
            )
            return dict(c)

        # Payroll entries single lookups / updates
        if q.startswith("update payroll_entries set status = 'on_hold'"):
            rid, cid, reason = str(args[0]), str(args[1]), args[2]
            e = self.db.entries.get(rid)
            if not e or e["cycle_id"] != cid:
                return None
            e["status"] = "ON_HOLD"
            e["hold_reason"] = reason
            e["updated_at"] = now
            return {"id": rid}

        if q.startswith("update payroll_entries set status = 'cleared'"):
            rid, cid, note = str(args[0]), str(args[1]), args[2]
            e = self.db.entries.get(rid)
            if not e or e["cycle_id"] != cid:
                return None
            e["status"] = "CLEARED"
            e["hold_reason"] = note
            e["updated_at"] = now
            return {"id": rid}

        if "select id, employee_id::text as employee_id, status, hold_reason from payroll_entries" in q:
            rid, cid = str(args[0]), str(args[1])
            e = self.db.entries.get(rid)
            if not e or e["cycle_id"] != cid:
                return None
            return dict(e)

        if "from payroll_entries pe join payroll_cycles pc" in q and "where pe.id = $1::uuid" in q:
            eid, emp_id = str(args[0]), str(args[1])
            e = self.db.entries.get(eid)
            if not e or e["employee_id"] != emp_id or e["status"] != "CLEARED":
                return None
            c = self.db.cycles.get(e["cycle_id"])
            if not c or c["status"] not in ("APPROVED", "DISBURSED"):
                return None
            u = self.db.users[emp_id]
            out = dict(e)
            out.update(
                employee_name=u["full_name"],
                employee_email=u["email"],
                department=self.db.departments.get(u["department_id"]),
                designation=u["designation"],
                pan_number=u["pan_number"],
                uan_number=u["uan_number"],
            )
            return out

        raise AssertionError(f"Unhandled fetchrow SQL: {q}")

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        q = self._norm(sql)

        if "from pt_state_slabs" in q:
            return []

        if "from holidays" in q:
            return []

        if "from users u left join departments d" in q:
            rows = []
            for u in self.db.users.values():
                if not u["is_active"]:
                    continue
                struct = next(
                    (s for s in self.db.salary_structures if s["employee_id"] == u["id"] and s["is_active"]),
                    None,
                )
                bank = next(
                    (b for b in self.db.bank_details if b["employee_id"] == u["id"] and b["is_primary"]),
                    None,
                )
                rows.append(
                    {
                        "employee_id": u["id"],
                        "full_name": u["full_name"],
                        "email": u["email"],
                        "department": self.db.departments.get(u["department_id"]),
                        "designation": u["designation"],
                        "joining_date": u["joining_date"],
                        "date_of_exit": u["date_of_exit"],
                        "pan_number": u["pan_number"],
                        "uan_number": u["uan_number"],
                        "aadhaar_number": u["aadhaar_number"],
                        "esic_ip_number": u["esic_ip_number"],
                        "father_or_spouse_name": u["father_or_spouse_name"],
                        "exit_reason_code": u["exit_reason_code"],
                        "structure_id": struct["id"] if struct else None,
                        "annual_ctc": struct["annual_ctc"] if struct else None,
                        "is_epf_eligible": struct["is_epf_eligible"] if struct else None,
                        "is_esi_eligible": struct["is_esi_eligible"] if struct else None,
                        "pt_state_code": struct["pt_state_code"] if struct else None,
                        "tax_regime": struct["tax_regime"] if struct else None,
                        "bank_id": bank["id"] if bank else None,
                        "payment_method": bank["payment_method"] if bank else None,
                        "bank_name": bank["bank_name"] if bank else None,
                        "ifsc_code": bank["ifsc_code"] if bank else None,
                    }
                )
            return rows

        if "from employee_salary_structures ess join users u" in q and "monthly_basic_pay" in q and "u.joining_date" in q:
            # Aggregator employee query
            emp_filter = None
            ref_cycle_id = None
            idx = 0
            if "u.id = any(" in q:
                emp_filter = {str(x) for x in args[idx]}
                idx += 1
            if "where pe.cycle_id =" in q:
                ref_cycle_id = str(args[idx])
                idx += 3  # ref_cycle_id, cycle_month, cycle_year

            held_in_ref: Optional[set[str]] = None
            already_paid_in_month: set[str] = set()
            if ref_cycle_id:
                held_in_ref = {
                    e["employee_id"]
                    for e in self.db.entries.values()
                    if e["cycle_id"] == ref_cycle_id
                    and (e["status"] == "ON_HOLD" or e["payout_status"] != "PAID")
                }
                for e in self.db.entries.values():
                    c = self.db.cycles.get(e["cycle_id"])
                    if (
                        c
                        and c["status"] == "DISBURSED"
                        and e["status"] == "CLEARED"
                        and e["payout_status"] == "PAID"
                    ):
                        already_paid_in_month.add(e["employee_id"])

            out = []
            for s in self.db.salary_structures:
                if not s["is_active"]:
                    continue
                eid = s["employee_id"]
                u = self.db.users.get(eid)
                if not u or not u["is_active"]:
                    continue
                if emp_filter is not None and eid not in emp_filter:
                    continue
                if held_in_ref is not None and (eid not in held_in_ref or eid in already_paid_in_month):
                    continue
                out.append(
                    {
                        "employee_id": eid,
                        "employee_name": u["full_name"],
                        "email": u["email"],
                        "joining_date": u["joining_date"],
                        "date_of_exit": u["date_of_exit"],
                        "pan_number": u["pan_number"],
                        "uan_number": u["uan_number"],
                        "esic_ip_number": u["esic_ip_number"],
                        "monthly_basic_pay": s["monthly_basic_pay"],
                        "monthly_hra": s["monthly_hra"],
                        "monthly_special_allowance": s["monthly_special_allowance"],
                        "monthly_da": s["monthly_da"],
                        "is_epf_eligible": s["is_epf_eligible"],
                        "epf_wage_cap_applied": s["epf_wage_cap_applied"],
                        "vpf_percentage": s["vpf_percentage"],
                        "is_esi_eligible": s["is_esi_eligible"],
                        "pt_state_code": s["pt_state_code"],
                        "tax_regime": s["tax_regime"],
                    }
                )
            return out

        if "from employee_salary_structures ess join users u" in q:
            out = []
            for s in self.db.salary_structures:
                if "ess.is_active = true" in q and not s["is_active"]:
                    continue
                if "ess.employee_id =" in q and s["employee_id"] != str(args[0]):
                    continue
                u = self.db.users[s["employee_id"]]
                row = dict(s)
                row["employee_name"] = u["full_name"]
                row["email"] = u["email"]
                out.append(row)
            return out

        if "from employee_tax_declarations etd join users u" in q:
            out = []
            for d in self.db.tax_declarations:
                if args and d["financial_year"] != args[0]:
                    continue
                row = dict(d)
                row["employee_name"] = self.db.users[d["employee_id"]]["full_name"]
                out.append(row)
            return out

        if "from employee_tax_declarations where employee_id = any" in q:
            emp_ids = {str(x) for x in args[0]}
            fy = args[1]
            return [
                dict(d)
                for d in self.db.tax_declarations
                if d["employee_id"] in emp_ids and d["financial_year"] == fy
            ]

        if "from employee_bank_details where employee_id = any" in q:
            emp_ids = {str(x) for x in args[0]}
            return [
                dict(b)
                for b in self.db.bank_details
                if b["employee_id"] in emp_ids and b["is_primary"]
            ]

        if "from payroll_entries pe join payroll_cycles pc" in q and "group by pe.employee_id" in q:
            return []

        if "from leaves l" in q:
            emp_ids = {str(x) for x in args[0]}
            start_dt, end_dt = args[1], args[2]
            return [
                {
                    "employee_id": lv["user_id"],
                    "leave_type": lv["leave_type"],
                    "start_date": lv["start_date"],
                    "end_date": lv["end_date"],
                    "half_day": lv["half_day"],
                }
                for lv in self.db.leaves
                if lv["user_id"] in emp_ids
                and lv["status"] == "approved"
                and lv["start_date"] <= end_dt
                and lv["end_date"] >= start_dt
            ]

        if "from attendance_regularisations" in q:
            return []

        if "from attendance" in q:
            emp_ids = {str(x) for x in args[0]}
            start_dt, end_dt = args[1], args[2]
            return [
                dict(a)
                for a in self.db.attendance
                if a["employee_id"] in emp_ids
                and start_dt <= a["work_date"] <= end_dt
            ]

        if "from reimbursements" in q:
            emp_ids = {str(x) for x in args[0]}
            cutoff = args[1]
            return [
                {
                    "reimbursement_id": r["id"],
                    "employee_id": r["user_id"],
                    "category": r["category"],
                    "subcategory": r["subcategory"],
                    "amount": r["amount"],
                    "expense_date": r["expense_date"],
                    "decided_at": None,
                }
                for r in self.db.reimbursements.values()
                if r["user_id"] in emp_ids
                and r["status"] == "approved"
                and r["paid_at"] is None
                and r["expense_date"] <= cutoff
            ]

        if "from employee_salary_advances esa join users u" in q:
            out = []
            for a in self.db.advances.values():
                if args and "esa.employee_id =" in q and a["employee_id"] != str(args[0]):
                    continue
                row = dict(a)
                row["employee_name"] = self.db.users[a["employee_id"]]["full_name"]
                out.append(row)
            return out

        if "from employee_salary_advances" in q and "for update" in q:
            ids = {str(x) for x in args[0]}
            return [dict(a) for a in self.db.advances.values() if a["id"] in ids]

        if "from employee_salary_advances" in q:
            emp_ids = {str(x) for x in args[0]}
            out = []
            for a in self.db.advances.values():
                if (
                    a["employee_id"] in emp_ids
                    and a["status"] == "ACTIVE"
                    and Decimal(str(a["remaining_balance"])) > 0
                ):
                    row = dict(a)
                    row["advance_id"] = a["id"]
                    out.append(row)
            return out

        if "from payroll_cycles pc left join" in q:
            out = []
            for c in self.db.cycles.values():
                entries = [e for e in self.db.entries.values() if e["cycle_id"] == c["id"]]
                row = dict(c)
                row["cleared_count"] = sum(1 for e in entries if e["status"] == "CLEARED")
                row["on_hold_count"] = sum(1 for e in entries if e["status"] == "ON_HOLD")
                out.append(row)
            return out

        if "select employee_id::text as employee_id, status, hold_reason from payroll_entries" in q:
            cid = str(args[0])
            return [
                {"employee_id": e["employee_id"], "status": e["status"], "hold_reason": e["hold_reason"]}
                for e in self.db.entries.values()
                if e["cycle_id"] == cid
            ]

        if "from payroll_entries pe join users u on u.id = pe.employee_id" in q:
            cid = str(args[0])
            out = []
            for e in self.db.entries.values():
                if e["cycle_id"] != cid:
                    continue
                u = self.db.users[e["employee_id"]]
                row = dict(e)
                row.update(
                    employee_name=u["full_name"],
                    employee_email=u["email"],
                    department=self.db.departments.get(u["department_id"]),
                    designation=u["designation"],
                    pan_number=u["pan_number"],
                    uan_number=u["uan_number"],
                )
                out.append(row)
            return out

        if "from payroll_cycle_approvals pca" in q:
            cid = str(args[0])
            out = []
            for a in sorted(self.db.approvals, key=lambda x: x["step_order"]):
                if a["cycle_id"] != cid:
                    continue
                row = dict(a)
                app_u = self.db.users.get(a["approver_id"]) if a["approver_id"] else None
                row["approver_name"] = app_u["full_name"] if app_u else None
                out.append(row)
            return out

        if "from payroll_cycle_approvals where cycle_id =" in q:
            cid = str(args[0])
            return [
                dict(a)
                for a in sorted(self.db.approvals, key=lambda x: x["step_order"])
                if a["cycle_id"] == cid
            ]

        if "from payroll_entries where cycle_id = $1::uuid and status = 'cleared'" in q:
            cid = str(args[0])
            return [
                dict(e)
                for e in self.db.entries.values()
                if e["cycle_id"] == cid and e["status"] == "CLEARED"
            ]

        if "from payroll_entries pe join payroll_cycles pc on pc.id = pe.cycle_id" in q and "where pe.employee_id =" in q:
            emp_id = str(args[0])
            out = []
            for e in self.db.entries.values():
                if e["employee_id"] != emp_id or e["status"] != "CLEARED":
                    continue
                c = self.db.cycles.get(e["cycle_id"])
                if not c or c["status"] not in ("APPROVED", "DISBURSED"):
                    continue
                row = dict(e)
                row["entry_id"] = e["id"]
                row["cycle_name"] = c["cycle_name"]
                row["cycle_month"] = c["cycle_month"]
                row["cycle_year"] = c["cycle_year"]
                row["cycle_type"] = c["cycle_type"]
                row["cycle_status"] = c["status"]
                out.append(row)
            return out

        raise AssertionError(f"Unhandled fetch SQL: {q}")

    async def execute(self, sql: str, *args: Any) -> str:
        q = self._norm(sql)
        now = datetime.now(timezone.utc)

        if q.startswith("update employee_salary_structures set is_active = false"):
            eid = str(args[0])
            for s in self.db.salary_structures:
                if s["employee_id"] == eid and s["is_active"]:
                    s["is_active"] = False
                    s["effective_to"] = args[1]
                    s["updated_at"] = now
            return "UPDATE"

        if q.startswith("update employee_bank_details set is_primary = false"):
            eid = str(args[0])
            for b in self.db.bank_details:
                if b["employee_id"] == eid and b["is_primary"]:
                    b["is_primary"] = False
                    b["updated_at"] = now
            return "UPDATE"

        if q.startswith("update payroll_cycles set status = 'calculating'"):
            self.db.cycles[str(args[0])]["status"] = "CALCULATING"
            return "UPDATE 1"

        if q.startswith("update payroll_cycles set status = 'draft'"):
            self.db.cycles[str(args[0])]["status"] = "DRAFT"
            return "UPDATE 1"

        if q.startswith("update payroll_cycles set status = 'review'"):
            self.db.cycles[str(args[0])]["status"] = "REVIEW"
            return "UPDATE 1"

        if q.startswith("update payroll_cycles set status = 'rejected'"):
            self.db.cycles[str(args[0])]["status"] = "REJECTED"
            return "UPDATE 1"

        if q.startswith("update payroll_cycles set status = 'approved'"):
            c = self.db.cycles[str(args[0])]
            c["status"] = "APPROVED"
            c["approved_by"] = str(args[1])
            return "UPDATE 1"

        if q.startswith("update payroll_cycles set status = 'disbursed'"):
            c = self.db.cycles[str(args[0])]
            c["status"] = "DISBURSED"
            c["bank_batch_reference"] = args[1]
            c["disbursed_at"] = now
            return "UPDATE 1"

        if q.startswith("delete from payroll_cycle_approvals"):
            cid = str(args[0])
            self.db.approvals = [a for a in self.db.approvals if a["cycle_id"] != cid]
            return "DELETE"

        if q.startswith("insert into payroll_cycle_approvals"):
            self.db.approvals.append(
                {
                    "id": str(uuid.uuid4()),
                    "cycle_id": str(args[0]),
                    "step_order": int(args[1]),
                    "role_required": args[2],
                    "approver_id": None,
                    "status": "PENDING",
                    "comments": None,
                    "decided_at": None,
                    "created_at": now,
                }
            )
            return "INSERT 1"

        if q.startswith("update payroll_cycle_approvals set approver_id ="):
            aid = str(args[0])
            for a in self.db.approvals:
                if a["id"] == aid:
                    a["approver_id"] = str(args[1])
                    a["status"] = args[2]
                    a["comments"] = args[3]
                    a["decided_at"] = now
            return "UPDATE 1"

        if q.startswith("insert into payroll_entries"):
            cid, eid = str(args[0]), str(args[1])
            existing = next(
                (e for e in self.db.entries.values() if e["cycle_id"] == cid and e["employee_id"] == eid),
                None,
            )
            rec_id = existing["id"] if existing else str(uuid.uuid4())
            self.db.entries[rec_id] = {
                "id": rec_id,
                "cycle_id": cid,
                "employee_id": eid,
                "status": args[2],
                "hold_reason": args[3],
                "total_calendar_days": args[4],
                "days_worked": args[5],
                "unpaid_leave_days": args[6],
                "unexcused_absent_days": args[7],
                "ncp_days": args[8],
                "earned_basic": args[9],
                "earned_hra": args[10],
                "earned_special_allowance": args[11],
                "earned_da": args[12],
                "gross_earnings": args[13],
                "epf_wages": args[14],
                "eps_wages": args[15],
                "esi_wages": args[16],
                "epf_employee_share": args[17],
                "eps_employer_share": args[18],
                "epf_employer_share": args[19],
                "edli_employer_share": args[20],
                "epf_admin_charges": args[21],
                "esi_employee_share": args[22],
                "esi_employer_share": args[23],
                "professional_tax": args[24],
                "tds_amount": args[25],
                "reimbursements_amount": args[26],
                "advance_recovery_amount": args[27],
                "net_payable": args[28],
                "gratuity_accrual_monthly": args[29],
                "total_employer_cost": args[30],
                "earnings_breakdown": args[31],
                "deductions_breakdown": args[32],
                "payslip_pdf_url": None,
                "bank_utr_number": None,
                "payout_status": existing["payout_status"] if existing else "PENDING",
                "paid_at": existing["paid_at"] if existing else None,
                "created_at": existing["created_at"] if existing else now,
                "updated_at": now,
            }
            return "INSERT 1"

        if q.startswith("delete from payroll_entries"):
            cid = str(args[0])
            keep = {str(x) for x in args[1]}
            to_del = [
                k for k, v in self.db.entries.items() if v["cycle_id"] == cid and v["employee_id"] not in keep
            ]
            for k in to_del:
                del self.db.entries[k]
            return "DELETE"

        if q.startswith("update payroll_entries set payout_status = 'paid'"):
            e = self.db.entries[str(args[0])]
            e["payout_status"] = "PAID"
            e["bank_utr_number"] = args[1]
            e["bank_verified"] = args[2] if len(args) > 2 else False
            e["paid_at"] = now
            e["updated_at"] = now
            return "UPDATE 1"

        if q.startswith("insert into payroll_payment_batches"):
            return "INSERT 1"

        if q.startswith("insert into payroll_audit_logs"):
            return "INSERT 1"

        if q.startswith("insert into payroll_statutory_remittances"):
            return "INSERT 1"

        if q.startswith("update reimbursements set status = 'paid'"):
            rids = {str(x) for x in args[0]}
            for rid in rids:
                r = self.db.reimbursements.get(rid)
                if r and r["status"] == "approved" and r["paid_at"] is None:
                    r["status"] = "paid"
                    r["paid_at"] = now
            return "UPDATE"

        if q.startswith("update employee_salary_advances set remaining_balance ="):
            adv = self.db.advances[str(args[0])]
            adv["remaining_balance"] = args[1]
            adv["paid_installments"] = args[2]
            adv["status"] = args[3]
            adv["updated_at"] = now
            return "UPDATE 1"

        raise AssertionError(f"Unhandled execute SQL: {q}")


class FakePool:
    def __init__(self, db: StatefulPayrollDB) -> None:
        self.db = db

    @asynccontextmanager
    async def acquire(self):
        yield FakeAsyncpgConn(self.db)


@pytest.fixture()
def env(monkeypatch):
    """Provides `(client, db, set_actor)` with isolated stateful DB and JWT bypass."""
    db = StatefulPayrollDB()
    pool = FakePool(db)
    monkeypatch.setattr("app.api.payroll_v2_routes.get_db_pool", lambda: pool)
    monkeypatch.setattr("app.db.get_db_pool", lambda: pool)

    async def _noop_db():
        return None

    monkeypatch.setattr("app.main.init_db", _noop_db)
    monkeypatch.setattr("app.main.close_db", _noop_db)

    current_user = {"id": db.hr_id, "email": "hr@aimploy.com", "role": "hr", "permissions": []}

    def set_actor(user_id: str, role: str, email: str = "test@aimploy.com"):
        current_user["id"] = user_id
        current_user["role"] = role
        current_user["email"] = email

    app.dependency_overrides[get_current_user] = lambda: dict(current_user)
    try:
        with TestClient(app) as client:
            yield client, db, set_actor
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 1. Pydantic Validation & Indian Compliance Constraints
# ---------------------------------------------------------------------------
class TestPayrollV2DTOValidations:
    def test_ifsc_validation_enforces_indian_11_char_format(self):
        valid = BankDetailsUpsertRequest(
            payment_method="BANK_TRANSFER",
            account_holder_name="Darryl M A",
            bank_name="HDFC Bank",
            account_number="501000123456",
            ifsc_code="hdfc0000240",
        )
        assert valid.ifsc_code == "HDFC0000240"

        with pytest.raises(ValueError, match="IFSC"):
            BankDetailsUpsertRequest(
                payment_method="BANK_TRANSFER",
                account_holder_name="Darryl M A",
                bank_name="HDFC Bank",
                account_number="501000123456",
                ifsc_code="HDFC1000240",  # 5th char must be '0'
            )

    def test_salary_structure_auto_resolves_esi_threshold(self):
        esi_emp = SalaryStructureUpsertRequest(
            employee_id=str(uuid.uuid4()),
            effective_from=date(2026, 4, 1),
            annual_ctc=Decimal("240000"),
            monthly_basic_pay=Decimal("10000"),
            monthly_hra=Decimal("4000"),
            monthly_special_allowance=Decimal("4000"),
        )
        assert esi_emp.is_esi_eligible is True

        non_esi_emp = SalaryStructureUpsertRequest(
            employee_id=str(uuid.uuid4()),
            effective_from=date(2026, 4, 1),
            annual_ctc=Decimal("1200000"),
            monthly_basic_pay=Decimal("50000"),
            monthly_hra=Decimal("20000"),
            monthly_special_allowance=Decimal("30000"),
        )
        assert non_esi_emp.is_esi_eligible is False

    def test_cycle_create_validates_date_order_and_regular_reference_rule(self):
        with pytest.raises(ValueError, match="start_date <= cutoff_date <= end_date"):
            CycleCreateRequest(
                cycle_month=11,
                cycle_year=2026,
                start_date=date(2026, 11, 10),
                cutoff_date=date(2026, 11, 5),
                end_date=date(2026, 11, 30),
            )

        with pytest.raises(ValueError, match="REGULAR"):
            CycleCreateRequest(
                cycle_month=11,
                cycle_year=2026,
                cycle_type="REGULAR",
                reference_cycle_id=str(uuid.uuid4()),
            )

    def test_rejection_requires_mandatory_comments_and_hold_requires_reason(self):
        with pytest.raises(ValueError, match="comments"):
            CycleApprovalActionRequest(decision="REJECTED", comments="   ")

        with pytest.raises(ValueError):
            EntryHoldRequest(reason="  ")

    def test_advance_auto_calculates_installments(self):
        adv = SalaryAdvanceCreateRequest(
            employee_id=str(uuid.uuid4()),
            principal_amount=Decimal("10000"),
            monthly_emi_amount=Decimal("3000"),
        )
        assert adv.total_installments == 4


# ---------------------------------------------------------------------------
# 2. End-to-End Cycle Lifecycle, Line-Item Hold, 3-Tier Approvals & Disbursement
# ---------------------------------------------------------------------------
class TestPayrollCycleStateMachineAndHoldPattern:
    def test_duplicate_regular_cycle_returns_409(self, env):
        client, db, _ = env
        r1 = client.post("/api/v2/payroll/cycles", json={"cycle_month": 11, "cycle_year": 2026})
        assert r1.status_code == 201
        assert r1.json()["cycle_name"] == "November 2026 Payroll"
        assert r1.json()["status"] == "DRAFT"

        r2 = client.post("/api/v2/payroll/cycles", json={"cycle_month": 11, "cycle_year": 2026})
        assert r2.status_code == 409

    def test_batch_calculation_and_line_item_hold_recomputes_cleared_totals_only(self, env):
        client, db, set_actor = env
        create_res = client.post("/api/v2/payroll/cycles", json={"cycle_month": 11, "cycle_year": 2026})
        cycle_id = create_res.json()["id"]

        calc_res = client.post(f"/api/v2/payroll/cycles/{cycle_id}/calculate", json={})
        assert calc_res.status_code == 200
        data = calc_res.json()
        assert data["cycle"]["status"] == "DRAFT"
        assert data["cycle"]["employee_count"] == 3
        assert data["cycle"]["cleared_count"] == 3
        assert data["cycle"]["on_hold_count"] == 0

        entries_by_emp = {e["employee_id"]: e for e in data["entries"]}
        darryl_entry = entries_by_emp[db.emp_darryl_id]
        ajmal_entry = entries_by_emp[db.emp_ajmal_id]
        damewan_entry = entries_by_emp[db.emp_damewan_id]

        # Verify Damewan's 2 LOP days prorated component-wise (9333.33 + 3733.33 + 3733.33 = 16799.99)
        assert damewan_entry["ncp_days"] == 2.0
        assert damewan_entry["gross_earnings"] == 16799.99
        assert round(
            damewan_entry["earned_basic"]
            + damewan_entry["earned_hra"]
            + damewan_entry["earned_special_allowance"]
            + damewan_entry["earned_da"],
            2,
        ) == damewan_entry["gross_earnings"]
        assert damewan_entry["advance_recovery_amount"] == 1000.0

        # Verify Ajmal's reimbursement (2,500) and advance EMI (5,000) captured
        assert ajmal_entry["reimbursements_amount"] == 2500.0
        assert ajmal_entry["advance_recovery_amount"] == 5000.0

        full_gross = data["cycle"]["total_gross"]
        full_net = data["cycle"]["total_net_payable"]
        assert full_gross == round(
            darryl_entry["gross_earnings"] + ajmal_entry["gross_earnings"] + damewan_entry["gross_earnings"],
            2,
        )

        # Place Ajmal ON_HOLD (Finance Manager can also place ON_HOLD)
        set_actor(db.finance_id, "finance")
        hold_res = client.patch(
            f"/api/v2/payroll/cycles/{cycle_id}/records/{ajmal_entry['id']}/hold",
            json={"reason": "Unverified laptop stand invoice"},
        )
        assert hold_res.status_code == 200
        held_data = hold_res.json()
        assert held_data["cycle"]["employee_count"] == 2
        assert held_data["cycle"]["cleared_count"] == 2
        assert held_data["cycle"]["on_hold_count"] == 1
        assert held_data["cycle"]["total_gross"] == round(
            darryl_entry["gross_earnings"] + damewan_entry["gross_earnings"], 2
        )
        assert held_data["cycle"]["total_net_payable"] == round(
            darryl_entry["net_payable"] + damewan_entry["net_payable"], 2
        )

        # Resolve an extra reimbursement while held & single-recalculate as HR
        extra_reim_id = str(uuid.uuid4())
        db.reimbursements[extra_reim_id] = {
            "id": extra_reim_id,
            "user_id": db.emp_ajmal_id,
            "category": "Internet",
            "subcategory": "Broadband",
            "amount": Decimal("1000.00"),
            "expense_date": date(2026, 11, 20),
            "status": "approved",
            "paid_at": None,
        }
        set_actor(db.hr_id, "hr")
        recalc_single = client.post(
            f"/api/v2/payroll/cycles/{cycle_id}/records/{ajmal_entry['id']}/recalculate"
        )
        assert recalc_single.status_code == 200
        rs_data = recalc_single.json()
        updated_ajmal = next(e for e in rs_data["entries"] if e["id"] == ajmal_entry["id"])
        assert updated_ajmal["reimbursements_amount"] == 3500.0
        # Still ON_HOLD until explicitly released, so cycle total stays 2 cleared employees
        assert updated_ajmal["status"] == "ON_HOLD"
        assert rs_data["cycle"]["employee_count"] == 2

        # Release Ajmal -> totals now include all 3 + the extra Rs 1,000 reimbursement
        rel_res = client.patch(
            f"/api/v2/payroll/cycles/{cycle_id}/records/{ajmal_entry['id']}/release",
            json={"note": "Invoice verified"},
        )
        assert rel_res.status_code == 200
        assert rel_res.json()["cycle"]["employee_count"] == 3
        assert rel_res.json()["cycle"]["total_net_payable"] == round(full_net + 1000.0, 2)

    def test_sequential_3_tier_approvals_disbursement_and_off_cycle_recovery(self, env):
        client, db, set_actor = env

        # 1. Create & Calculate Nov 2026 Regular Cycle
        cycle_id = client.post(
            "/api/v2/payroll/cycles", json={"cycle_month": 11, "cycle_year": 2026}
        ).json()["id"]
        calc_data = client.post(f"/api/v2/payroll/cycles/{cycle_id}/calculate").json()
        entries_by_emp = {e["employee_id"]: e for e in calc_data["entries"]}
        ajmal_entry = entries_by_emp[db.emp_ajmal_id]

        # Put Ajmal ON_HOLD before submission
        client.patch(
            f"/api/v2/payroll/cycles/{cycle_id}/records/{ajmal_entry['id']}/hold",
            json={"reason": "Attendance regularisation pending"},
        )

        # 2. Submit for 3-Tier Review
        sub_res = client.post(f"/api/v2/payroll/cycles/{cycle_id}/submit")
        assert sub_res.status_code == 200
        sub_data = sub_res.json()
        assert sub_data["cycle"]["status"] == "REVIEW"
        assert [a["role_required"] for a in sub_data["approvals"]] == [
            "hr_admin",
            "finance_manager",
            "cfo",
        ]

        # Out-of-order check: Finance & CFO cannot approve Step 1 (hr_admin)
        set_actor(db.finance_id, "finance")
        assert (
            client.post(
                f"/api/v2/payroll/cycles/{cycle_id}/approve",
                json={"decision": "APPROVED"},
            ).status_code
            == 403
        )

        set_actor(db.cfo_id, "cfo")
        assert (
            client.post(
                f"/api/v2/payroll/cycles/{cycle_id}/approve",
                json={"decision": "APPROVED"},
            ).status_code
            == 403
        )

        # Step 1: HR approves
        set_actor(db.hr_id, "hr")
        s1 = client.post(
            f"/api/v2/payroll/cycles/{cycle_id}/approve",
            json={"decision": "APPROVED", "comments": "Attendance & LOP verified"},
        )
        assert s1.status_code == 200
        assert s1.json()["cycle"]["status"] == "REVIEW"

        # HR cannot approve Step 2 (finance_manager)
        assert (
            client.post(
                f"/api/v2/payroll/cycles/{cycle_id}/approve",
                json={"decision": "APPROVED"},
            ).status_code
            == 403
        )

        # Step 2: Finance approves
        set_actor(db.finance_id, "finance")
        s2 = client.post(
            f"/api/v2/payroll/cycles/{cycle_id}/approve",
            json={"decision": "APPROVED", "comments": "TDS & EPF challan reconciled"},
        )
        assert s2.status_code == 200
        assert s2.json()["cycle"]["status"] == "REVIEW"

        # CFO cannot disburse before approving Step 3
        set_actor(db.cfo_id, "cfo")
        assert (
            client.post(
                f"/api/v2/payroll/cycles/{cycle_id}/mark-disbursed",
                json={"bank_batch_reference": "NEFT-20261130-01"},
            ).status_code
            == 409
        )

        # Step 3: CFO approves -> APPROVED & locked
        s3 = client.post(
            f"/api/v2/payroll/cycles/{cycle_id}/approve",
            json={"decision": "APPROVED", "comments": "Cleared for NEFT release"},
        )
        assert s3.status_code == 200
        assert s3.json()["cycle"]["status"] == "APPROVED"

        # Locked check: Cannot hold or release once APPROVED
        set_actor(db.hr_id, "hr")
        assert (
            client.patch(
                f"/api/v2/payroll/cycles/{cycle_id}/records/{ajmal_entry['id']}/release"
            ).status_code
            == 409
        )

        # Non-CFO cannot mark-disbursed
        set_actor(db.finance_id, "finance")
        assert (
            client.post(
                f"/api/v2/payroll/cycles/{cycle_id}/mark-disbursed",
                json={"bank_batch_reference": "NEFT-20261130-01"},
            ).status_code
            == 403
        )

        # 3. CFO Disburses Cycle
        set_actor(db.cfo_id, "cfo")
        disb_res = client.post(
            f"/api/v2/payroll/cycles/{cycle_id}/mark-disbursed",
            json={
                "bank_batch_reference": "HDFC-NEFT-20261130-01",
                "utr_numbers": {db.emp_darryl_id: "UTR-DARRYL-001"},
            },
        )
        assert disb_res.status_code == 200
        assert disb_res.json()["cycle"]["status"] == "DISBURSED"

        # Duplicate disbursement blocked
        assert (
            client.post(
                f"/api/v2/payroll/cycles/{cycle_id}/mark-disbursed",
                json={"bank_batch_reference": "HDFC-NEFT-20261130-02"},
            ).status_code
            == 409
        )

        # Verify selective settlement:
        # - Darryl & Damewan PAID; Ajmal (ON_HOLD) stays PENDING
        post_entries = {
            e["employee_id"]: e
            for e in client.get(f"/api/v2/payroll/cycles/{cycle_id}").json()["entries"]
        }
        assert post_entries[db.emp_darryl_id]["payout_status"] == "PAID"
        assert post_entries[db.emp_darryl_id]["bank_utr_number"] == "UTR-DARRYL-001"
        assert post_entries[db.emp_damewan_id]["payout_status"] == "PAID"
        assert post_entries[db.emp_damewan_id]["bank_utr_number"] == "HDFC-NEFT-20261130-01"
        assert post_entries[db.emp_ajmal_id]["payout_status"] == "PENDING"
        assert post_entries[db.emp_ajmal_id]["paid_at"] is None

        # - Reimbursements: Darryl's paid; Ajmal's STILL approved & unpaid
        assert db.reimbursements[db.reim_darryl_id]["status"] == "paid"
        assert db.reimbursements[db.reim_darryl_id]["paid_at"] is not None
        assert db.reimbursements[db.reim_ajmal_id]["status"] == "approved"
        assert db.reimbursements[db.reim_ajmal_id]["paid_at"] is None

        # - Salary Advances: Damewan's paid off -> COMPLETED; Ajmal's untouched at Rs 15,000
        assert db.advances[db.adv_damewan_id]["remaining_balance"] == Decimal("0.00")
        assert db.advances[db.adv_damewan_id]["paid_installments"] == 3
        assert db.advances[db.adv_damewan_id]["status"] == "COMPLETED"
        assert db.advances[db.adv_ajmal_id]["remaining_balance"] == Decimal("15000.00")
        assert db.advances[db.adv_ajmal_id]["paid_installments"] == 0
        assert db.advances[db.adv_ajmal_id]["status"] == "ACTIVE"

        # 4. Run OFF_CYCLE for Nov 2026 referencing Regular Cycle -> picks up ONLY Ajmal!
        set_actor(db.hr_id, "hr")
        off_res = client.post(
            "/api/v2/payroll/cycles",
            json={
                "cycle_month": 11,
                "cycle_year": 2026,
                "cycle_type": "OFF_CYCLE",
                "reference_cycle_id": cycle_id,
            },
        )
        assert off_res.status_code == 201
        off_id = off_res.json()["id"]

        off_calc = client.post(f"/api/v2/payroll/cycles/{off_id}/calculate").json()
        assert off_calc["cycle"]["employee_count"] == 1
        assert len(off_calc["entries"]) == 1
        assert off_calc["entries"][0]["employee_id"] == db.emp_ajmal_id
        assert off_calc["entries"][0]["reimbursements_amount"] == 2500.0
        assert off_calc["entries"][0]["advance_recovery_amount"] == 5000.0

        # Fast-track Off-Cycle approval as Admin and Disburse as CFO
        set_actor(str(uuid.uuid4()), "admin")
        client.post(f"/api/v2/payroll/cycles/{off_id}/submit")
        client.post(f"/api/v2/payroll/cycles/{off_id}/approve", json={"decision": "APPROVED"})
        client.post(f"/api/v2/payroll/cycles/{off_id}/approve", json={"decision": "APPROVED"})
        client.post(f"/api/v2/payroll/cycles/{off_id}/approve", json={"decision": "APPROVED"})

        set_actor(db.cfo_id, "cfo")
        off_disb = client.post(
            f"/api/v2/payroll/cycles/{off_id}/mark-disbursed",
            json={"bank_batch_reference": "HDFC-OFFCYCLE-20261203"},
        )
        assert off_disb.status_code == 200
        assert db.reimbursements[db.reim_ajmal_id]["status"] == "paid"
        assert db.advances[db.adv_ajmal_id]["remaining_balance"] == Decimal("10000.00")
        assert db.advances[db.adv_ajmal_id]["paid_installments"] == 1

    def test_rejection_resets_cycle_and_preserves_manual_holds_on_recalculate(self, env):
        client, db, set_actor = env
        cycle_id = client.post(
            "/api/v2/payroll/cycles", json={"cycle_month": 12, "cycle_year": 2026}
        ).json()["id"]
        calc = client.post(f"/api/v2/payroll/cycles/{cycle_id}/calculate").json()
        ajmal_rec = next(e for e in calc["entries"] if e["employee_id"] == db.emp_ajmal_id)

        client.patch(
            f"/api/v2/payroll/cycles/{cycle_id}/records/{ajmal_rec['id']}/hold",
            json={"reason": "Verify bonus"},
        )
        client.post(f"/api/v2/payroll/cycles/{cycle_id}/submit")
        client.post(f"/api/v2/payroll/cycles/{cycle_id}/approve", json={"decision": "APPROVED"})

        set_actor(db.finance_id, "finance")
        rej = client.post(
            f"/api/v2/payroll/cycles/{cycle_id}/approve",
            json={"decision": "REJECTED", "comments": "Please update PT slab for MH transfer"},
        )
        assert rej.status_code == 200
        assert rej.json()["cycle"]["status"] == "REJECTED"

        # HR recalculates -> returns to DRAFT, clears stale approvals, keeps Ajmal ON_HOLD
        set_actor(db.hr_id, "hr")
        recalc = client.post(
            f"/api/v2/payroll/cycles/{cycle_id}/calculate",
            json={"preserve_holds": True},
        )
        assert recalc.status_code == 200
        r_data = recalc.json()
        assert r_data["cycle"]["status"] == "DRAFT"
        assert r_data["cycle"]["on_hold_count"] == 1
        assert r_data["approvals"] == []


# ---------------------------------------------------------------------------
# 3. Employee Self-Service (ESS) & Master Configuration Routes
# ---------------------------------------------------------------------------
class TestESSAndMasterConfigurationAPIs:
    def test_ess_payslip_isolation_and_provisional_preview(self, env):
        client, db, set_actor = env

        # Create, calculate, hold Ajmal, approve & disburse Nov 2026
        cycle_id = client.post(
            "/api/v2/payroll/cycles", json={"cycle_month": 11, "cycle_year": 2026}
        ).json()["id"]
        calc = client.post(f"/api/v2/payroll/cycles/{cycle_id}/calculate").json()
        ajmal_rec = next(e for e in calc["entries"] if e["employee_id"] == db.emp_ajmal_id)
        client.patch(
            f"/api/v2/payroll/cycles/{cycle_id}/records/{ajmal_rec['id']}/hold",
            json={"reason": "Pending document"},
        )
        set_actor(str(uuid.uuid4()), "admin")
        client.post(f"/api/v2/payroll/cycles/{cycle_id}/submit")
        client.post(f"/api/v2/payroll/cycles/{cycle_id}/approve", json={"decision": "APPROVED"})
        client.post(f"/api/v2/payroll/cycles/{cycle_id}/approve", json={"decision": "APPROVED"})
        client.post(f"/api/v2/payroll/cycles/{cycle_id}/approve", json={"decision": "APPROVED"})
        client.post(
            f"/api/v2/payroll/cycles/{cycle_id}/mark-disbursed",
            json={"bank_batch_reference": "BATCH-01"},
        )

        # Darryl sees his finalized payslip
        set_actor(db.emp_darryl_id, "employee", "darryl@aimploy.com")
        darryl_list = client.get("/api/v2/payroll/me/payslips").json()
        assert len(darryl_list) == 1
        darryl_entry_id = darryl_list[0]["entry_id"]

        darryl_detail = client.get(f"/api/v2/payroll/me/payslips/{darryl_entry_id}")
        assert darryl_detail.status_code == 200
        assert darryl_detail.json()["entry"]["employee_id"] == db.emp_darryl_id
        assert darryl_detail.json()["company"]["tan_number"] == "BLRA12345C"

        # Ajmal (ON_HOLD) sees 0 finalized payslips AND gets 404 on Darryl's entry ID
        set_actor(db.emp_ajmal_id, "employee", "ajmal@aimploy.com")
        assert client.get("/api/v2/payroll/me/payslips").json() == []
        assert client.get(f"/api/v2/payroll/me/payslips/{darryl_entry_id}").status_code == 404

        # Employee blocked from admin routes (403)
        assert client.get("/api/v2/payroll/cycles").status_code == 403
        assert client.get("/api/v2/payroll/company-statutory").status_code == 403

        # Provisional preview runs read-only calculation
        prev = client.get("/api/v2/payroll/me/provisional-preview?month=11&year=2026&as_of_date=2026-11-20")
        assert prev.status_code == 200
        p_json = prev.json()
        assert p_json["has_salary_structure"] is True
        assert p_json["calculation"]["gross_earnings"] == 40000.0

        # Employee submits tax declaration -> Finance verifies
        decl_res = client.put(
            "/api/v2/payroll/me/tax-declarations",
            json={
                "financial_year": "2026-2027",
                "regime": "OLD",
                "section_80c_amount": 150000,
                "section_80d_amount": 25000,
                "annual_rent_paid": 240000,
            },
        )
        assert decl_res.status_code == 200
        decl_id = decl_res.json()["id"]
        assert decl_res.json()["is_verified"] is False

        set_actor(db.finance_id, "finance")
        ver_res = client.patch(
            f"/api/v2/payroll/tax-declarations/{decl_id}/verify",
            json={"is_verified": True},
        )
        assert ver_res.status_code == 200
        assert ver_res.json()["is_verified"] is True

    def test_master_crud_salary_revision_bank_and_statutory_profile(self, env):
        client, db, set_actor = env
        set_actor(db.hr_id, "hr")

        # Update company statutory
        comp = client.put(
            "/api/v2/payroll/company-statutory",
            json={
                "company_name": "Aimploy Technologies India Pvt Ltd",
                "tan_number": "BLRA12345C",
                "pan_number": "AABCA1234F",
                "epfo_establishment_code": "BGBNG0012345000",
                "registered_address": "Outer Ring Road, Bengaluru",
            },
        )
        assert comp.status_code == 200
        assert comp.json()["company_name"] == "Aimploy Technologies India Pvt Ltd"

        # Revise Darryl's salary structure -> deactivates old, creates new active
        rev = client.post(
            "/api/v2/payroll/salary-structures",
            json={
                "employee_id": db.emp_darryl_id,
                "effective_from": "2026-12-01",
                "annual_ctc": 1500000,
                "monthly_basic_pay": 62500,
                "monthly_hra": 25000,
                "monthly_special_allowance": 37500,
                "pt_state_code": "KA",
                "tax_regime": "NEW",
            },
        )
        assert rev.status_code == 201
        assert rev.json()["monthly_gross"] == 125000.0
        active_for_darryl = [
            s for s in db.salary_structures if s["employee_id"] == db.emp_darryl_id and s["is_active"]
        ]
        assert len(active_for_darryl) == 1

        # Update employee statutory KYC profile
        kyc = client.put(
            f"/api/v2/payroll/employees/{db.emp_darryl_id}/statutory-profile",
            json={
                "pan_number": "BGLPD1234K",
                "uan_number": "101234567890",
                "aadhaar_number": "123412341234",
            },
        )
        assert kyc.status_code == 200
        assert kyc.json()["aadhaar_number"] == "123412341234"

    def test_establishment_esic_gate_bypasses_deductions_when_uncovered(self, env):
        client, db, set_actor = env
        set_actor(str(uuid.uuid4()), "admin")

        # Disable ESIC at company level (establishment < 10 employees / uncovered)
        comp = client.put(
            "/api/v2/payroll/company-statutory",
            json={
                "company_name": "Aimploy Technologies India Pvt Ltd",
                "tan_number": "BLRA12345C",
                "pan_number": "AABCA1234F",
                "epfo_establishment_code": "BGBNG0012345000",
                "is_esic_applicable": False,
                "registered_address": "Outer Ring Road, Bengaluru",
            },
        )
        assert comp.status_code == 200
        assert comp.json()["is_esic_applicable"] is False

        # Create & calculate cycle for Nov 2026
        cid = client.post(
            "/api/v2/payroll/cycles", json={"cycle_month": 11, "cycle_year": 2026}
        ).json()["id"]
        calc = client.post(f"/api/v2/payroll/cycles/{cid}/calculate").json()
        by_emp = {e["employee_id"]: e for e in calc["entries"]}

        # Damewan has gross < 21000 (16800.0) but establishment is NOT covered -> 0 ESIC
        damewan_entry = by_emp[db.emp_damewan_id]
        assert damewan_entry["esi_employee_share"] == 0.0
        assert damewan_entry["esi_employer_share"] == 0.0
        assert calc["cycle"]["total_esi_employee"] == 0.0
        assert calc["cycle"]["total_esi_employer"] == 0.0
