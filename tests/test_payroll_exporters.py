"""
Offline Unit & API Integration Tests for Phase 5 Compliance Exporters & PDF Payslips.

Runs 100% locally in memory — ZERO calls to Neon Cloud PostgreSQL.
Verifies:
  1. EPFO ECR `#~#` delimited text format, UAN validation, and EE == EPS + ER checksums
  2. Corporate Bank NEFT / RTGS CSV format, RBI Rs. 2L RTGS threshold, IFSC validation
  3. Income Tax Form 24Q e-TDS (`FH^`, `BH^`, `CD^`, `DD^`) quarterly mapping & PAN validation
  4. Pure-Python Branded A4 Indian Payslip PDF parsed end-to-end with `pypdf.PdfReader`
  5. FastAPI download endpoints, status gating (`APPROVED`/`DISBURSED`), `ON_HOLD` exclusion, and RBAC
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pypdf import PdfReader

from app.features.auth import get_current_user
from app.features.payroll_v2.exporters import (
    amount_to_indian_words,
    build_bank_neft_csv,
    build_epfo_ecr_content,
    build_form24q_content,
    format_inr,
    get_tax_quarter_and_years,
    render_indian_payslip_pdf,
    sanitize_ecr_member_name,
)
from app.main import app
from tests.test_payroll_v2_workflow import FakePool, StatefulPayrollDB


# ---------------------------------------------------------------------------
# 1. Pure Exporter & Formatting Unit Tests
# ---------------------------------------------------------------------------
class TestEPFOECRExporter:
    def test_ecr_format_delimiter_and_checksum_invariant(self):
        rows = [
            {
                "full_name": "Darryl M. A. #1",
                "uan_number": "101234567890",
                "gross_earnings": Decimal("100000.00"),
                "epf_wages": Decimal("15000.00"),
                "eps_wages": Decimal("15000.00"),
                "epf_employee_share": Decimal("1800.00"),
                "eps_employer_share": Decimal("1250.00"),
                "epf_employer_share": Decimal("550.00"),
                "ncp_days": Decimal("0.0"),
                "deductions_breakdown": {"edli_wages": 15000.0},
            },
            {
                "full_name": "Damewan Bareh",
                "uan_number": "105555555555",
                "gross_earnings": Decimal("16800.00"),
                "epf_wages": Decimal("9333.00"),
                "eps_wages": Decimal("9333.00"),
                "epf_employee_share": Decimal("1120.00"),
                "eps_employer_share": Decimal("777.00"),
                "epf_employer_share": Decimal("343.00"),
                "ncp_days": Decimal("2.0"),
                "deductions_breakdown": {},
            },
        ]

        bundle = build_epfo_ecr_content(
            rows,
            establishment_code="BGBNG0012345000",
            cycle_year=2026,
            cycle_month=11,
        )
        assert bundle.filename == "EPFO_ECR_BGBNG0012345000_2026_11.txt"
        assert bundle.member_count == 2
        lines = bundle.content.strip().splitlines()
        assert len(lines) == 2

        for line in lines:
            cols = line.split("#~#")
            assert len(cols) == 11
            ee_share = int(cols[6])
            eps_share = int(cols[7])
            er_share = int(cols[8])
            assert ee_share == eps_share + er_share

        first_cols = lines[0].split("#~#")
        assert first_cols[0] == "101234567890"
        assert first_cols[1] == "DARRYL M A"
        assert first_cols[6:11] == ["1800", "1250", "550", "0", "0"]

    def test_ecr_rejects_missing_uan_with_422(self):
        rows = [
            {
                "full_name": "Missing UAN Staff",
                "uan_number": None,
                "gross_earnings": Decimal("30000.00"),
                "epf_wages": Decimal("15000.00"),
                "eps_wages": Decimal("15000.00"),
                "epf_employee_share": Decimal("1800.00"),
                "eps_employer_share": Decimal("1250.00"),
                "epf_employer_share": Decimal("550.00"),
                "ncp_days": Decimal("0.0"),
            }
        ]
        with pytest.raises(HTTPException) as exc:
            build_epfo_ecr_content(
                rows,
                establishment_code="BGBNG0012345000",
                cycle_year=2026,
                cycle_month=11,
            )
        assert exc.value.status_code == 422
        assert "Missing UAN Staff" in exc.value.detail

    def test_sanitize_member_name(self):
        assert sanitize_ecr_member_name("Ajmal#~#Asharudeen_99!") == "AJMAL ASHARUDEEN"


class TestBankNEFTExporter:
    def test_neft_and_rtgs_routing_and_csv_sanitization(self):
        rows = [
            {
                "id": str(uuid.uuid4()),
                "full_name": "Darryl M A",
                "account_holder_name": "Darryl M A",
                "email": "darryl@aimploy.com",
                "payment_method": "BANK_TRANSFER",
                "bank_name": "HDFC Bank",
                "account_number": "501000198765",
                "ifsc_code": "HDFC0000240",
                "net_payable": Decimal("88973.00"),
            },
            {
                "id": str(uuid.uuid4()),
                "full_name": "=Executive Director",
                "account_holder_name": "=Executive Director",
                "email": "exec@aimploy.com",
                "payment_method": "BANK_TRANSFER",
                "bank_name": "ICICI Bank",
                "account_number": "000105009876",
                "ifsc_code": "ICIC0000001",
                "net_payable": Decimal("250000.00"),
            },
        ]
        bundle = build_bank_neft_csv(
            rows,
            cycle_month=11,
            cycle_year=2026,
            cycle_type="REGULAR",
            cycle_name="November 2026 Payroll",
        )
        assert bundle.filename == "Aimploy_Bank_Payout_2026_11_REGULAR.csv"
        assert bundle.beneficiary_count == 2
        assert bundle.total_payout_amount == Decimal("338973.00")

        parsed = list(csv.reader(io.StringIO(bundle.content)))
        assert parsed[0][0:7] == [
            "Sr No",
            "Payment Mode",
            "Beneficiary Name",
            "Beneficiary Account Number",
            "IFSC Code",
            "Bank Name",
            "Amount (INR)",
        ]
        assert parsed[1][1] == "NEFT"
        assert parsed[2][1] == "RTGS"
        # Formula injection escaped cleanly
        assert parsed[2][2].startswith("'")

    def test_bank_csv_rejects_invalid_ifsc_with_422(self):
        rows = [
            {
                "id": str(uuid.uuid4()),
                "full_name": "Bad IFSC Staff",
                "payment_method": "BANK_TRANSFER",
                "account_number": "1234567890",
                "ifsc_code": "INVALID1234",
                "net_payable": Decimal("40000.00"),
            }
        ]
        with pytest.raises(HTTPException) as exc:
            build_bank_neft_csv(
                rows,
                cycle_month=11,
                cycle_year=2026,
                cycle_type="REGULAR",
                cycle_name="Nov 2026",
            )
        assert exc.value.status_code == 422
        assert "Bad IFSC Staff" in exc.value.detail


class TestForm24QExporter:
    def test_quarter_mapping_across_indian_fy(self):
        assert get_tax_quarter_and_years(5, 2026) == ("Q1", "2026-2027", "2027-2028")
        assert get_tax_quarter_and_years(8, 2026) == ("Q2", "2026-2027", "2027-2028")
        assert get_tax_quarter_and_years(11, 2026) == ("Q3", "2026-2027", "2027-2028")
        assert get_tax_quarter_and_years(2, 2027) == ("Q4", "2026-2027", "2027-2028")

    def test_form24q_records_and_tax_decomposition(self):
        rows = [
            {
                "full_name": "Darryl M A",
                "pan_number": "BGLPD1234K",
                "gross_earnings": Decimal("100000.00"),
                "tds_amount": Decimal("6240.00"),
                "deductions_breakdown": {
                    "tax_regime": "NEW",
                    "tax_breakdown": {
                        "regime": "NEW",
                        "projected_annual_gross": "1200000.00",
                        "standard_deduction": "75000.00",
                        "hra_exemption": "0.00",
                        "section_80c_allowed": "0.00",
                        "section_80d_allowed": "0.00",
                        "section_80ccd_1b_allowed": "0.00",
                        "section_24b_allowed": "0.00",
                        "taxable_income": "1125000.00",
                        "tax_after_rebate": "72000.00",
                        "surcharge": "0.00",
                        "health_and_education_cess": "2880.00",
                        "annual_tax_liability": "74880.00",
                    },
                },
            },
            {
                "full_name": "Damewan Bareh",
                "pan_number": None,
                "gross_earnings": Decimal("18000.00"),
                "tds_amount": Decimal("0.00"),
                "deductions_breakdown": {"tax_regime": "NEW"},
            },
        ]

        bundle = build_form24q_content(
            rows,
            company_name="Aimploy Technologies India Private Limited",
            tan_number="BLRA12345C",
            corporate_pan="AABCA1234F",
            registered_address="Bengaluru, KA",
            cycle_name="November 2026 Payroll",
            cycle_month=11,
            cycle_year=2026,
            payment_date=date(2026, 11, 30),
        )
        assert bundle.quarter == "Q3"
        assert bundle.financial_year == "2026-2027"
        assert bundle.assessment_year == "2027-2028"
        assert bundle.deductee_count == 2
        assert bundle.total_tds_deducted == Decimal("6240.00")

        lines = bundle.content.strip().splitlines()
        assert lines[0].startswith("FH^1^24Q^BLRA12345C^AABCA1234F^2026-2027^2027-2028^Q3^")
        assert lines[1].startswith("BH^1^November 2026 Payroll^30-11-2026^2^118000.00^6000.00^0.00^240.00^6240.00")
        assert lines[2].startswith("CD^1^192B^200^30-11-2026^6240.00^")
        assert lines[3].startswith("DD^1^192^BGLPD1234K^DARRYL M A^30-11-2026^100000.00^6000.00^0.00^240.00^6240.00^NEW^")
        assert lines[4].startswith("DD^2^192^PANNOTAVBL^DAMEWAN BAREH^")


class TestPayslipPDFGenerator:
    def test_indian_rupee_formatting_and_words(self):
        assert format_inr(Decimal("1234567.89")) == "Rs. 12,34,567.89"
        assert format_inr(Decimal("88973")) == "Rs. 88,973.00"
        assert amount_to_indian_words(Decimal("0")) == "Rupees Zero Only"
        assert (
            amount_to_indian_words(Decimal("125450"))
            == "Rupees One Lakh Twenty Five Thousand Four Hundred Fifty Only"
        )
        assert (
            amount_to_indian_words(Decimal("88973.50"))
            == "Rupees Eighty Eight Thousand Nine Hundred Seventy Three And Fifty Paise Only"
        )

    def test_pdf_bytes_valid_and_readable_via_pypdf(self):
        bundle = render_indian_payslip_pdf(
            company={
                "company_name": "Aimploy Technologies India Private Limited",
                "registered_address": "Prestige Tech Park, Bengaluru 560103",
                "pan_number": "AABCA1234F",
                "tan_number": "BLRA12345C",
                "epfo_establishment_code": "BGBNG0012345000",
            },
            employee={
                "full_name": "Darryl M A",
                "designation": "Senior Software Engineer",
                "department": "Engineering",
                "joining_date": date(2024, 4, 1),
                "pan_number": "BGLPD1234K",
                "uan_number": "101234567890",
                "esic_ip_number": None,
            },
            cycle={
                "cycle_name": "November 2026 Payroll",
                "cycle_month": 11,
                "cycle_year": 2026,
            },
            entry={
                "total_calendar_days": 30,
                "days_worked": Decimal("30.0"),
                "unpaid_leave_days": Decimal("0.0"),
                "unexcused_absent_days": Decimal("0.0"),
                "ncp_days": Decimal("0.0"),
                "earned_basic": Decimal("50000.00"),
                "earned_hra": Decimal("20000.00"),
                "earned_special_allowance": Decimal("30000.00"),
                "earned_da": Decimal("0.00"),
                "reimbursements_amount": Decimal("1200.00"),
                "gross_earnings": Decimal("100000.00"),
                "epf_employee_share": Decimal("1800.00"),
                "epf_employer_share": Decimal("550.00"),
                "eps_employer_share": Decimal("1250.00"),
                "edli_employer_share": Decimal("75.00"),
                "epf_admin_charges": Decimal("75.00"),
                "esi_employee_share": Decimal("0.00"),
                "esi_employer_share": Decimal("0.00"),
                "professional_tax": Decimal("200.00"),
                "tds_amount": Decimal("6240.00"),
                "advance_recovery_amount": Decimal("0.00"),
                "net_payable": Decimal("92960.00"),
                "gratuity_accrual_monthly": Decimal("2403.85"),
                "total_employer_cost": Decimal("104353.85"),
                "payout_status": "PAID",
                "bank_utr_number": "UTR-DARRYL-001",
                "deductions_breakdown": {"tax_regime": "NEW"},
            },
            bank={
                "bank_name": "HDFC Bank",
                "account_number": "501000198765",
                "ifsc_code": "HDFC0000240",
            },
        )

        assert bundle.filename == "Payslip_Darryl_M_A_2026_11.pdf"
        assert bundle.pdf_bytes.startswith(b"%PDF-1.4")
        assert bundle.pdf_bytes.rstrip().endswith(b"%%EOF")

        reader = PdfReader(io.BytesIO(bundle.pdf_bytes), strict=True)
        assert len(reader.pages) == 1
        extracted = reader.pages[0].extract_text() or ""
        assert "Aimploy Technologies India Private Limited" in extracted
        assert "Darryl M A" in extracted
        assert "BGLPD1234K" in extracted
        assert "101234567890" in extracted
        assert "HDFC0000240" in extracted
        assert "Rs. 92,960.00" in extracted
        assert "UTR-DARRYL-001" in extracted


# ---------------------------------------------------------------------------
# 2. FastAPI Download Endpoints, Status Guards, Hold Exclusion & RBAC Tests
# ---------------------------------------------------------------------------
class ExporterStatefulConn:
    """Extends FakeAsyncpgConn queries with Phase 5 JOIN select patterns."""

    def __init__(self, base_conn) -> None:
        self._base = base_conn
        self.db = base_conn.db

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        q = " ".join(sql.split()).lower()
        if "select id, cycle_month, cycle_year, status from payroll_cycles" in q or (
            "select id, cycle_name, cycle_month, cycle_year" in q and "from payroll_cycles" in q
        ):
            c = self.db.cycles.get(str(args[0]))
            return dict(c) if c else None

        if "from payroll_entries pe join payroll_cycles pc on pc.id = pe.cycle_id" in q and "left join employee_bank_details" in q:
            eid = str(args[0])
            e = self.db.entries.get(eid)
            if not e or e["status"] != "CLEARED":
                return None
            c = self.db.cycles.get(e["cycle_id"])
            if not c or c["status"] not in ("APPROVED", "DISBURSED"):
                return None
            if len(args) >= 2 and "pe.employee_id =" in q:
                if e["employee_id"] != str(args[1]):
                    return None
            if "pe.cycle_id =" in q:
                target_cid = str(args[-1])
                if e["cycle_id"] != target_cid:
                    return None
            u = self.db.users[e["employee_id"]]
            bank = next(
                (b for b in self.db.bank_details if b["employee_id"] == e["employee_id"] and b["is_primary"]),
                {},
            )
            out = dict(e)
            out.update(
                cycle_name=c["cycle_name"],
                cycle_month=c["cycle_month"],
                cycle_year=c["cycle_year"],
                cycle_status=c["status"],
                full_name=u["full_name"],
                email=u["email"],
                designation=u["designation"],
                joining_date=u["joining_date"],
                pan_number=u["pan_number"],
                uan_number=u["uan_number"],
                esic_ip_number=u["esic_ip_number"],
                department=self.db.departments.get(u["department_id"]),
                bank_name=bank.get("bank_name"),
                account_number=bank.get("account_number"),
                ifsc_code=bank.get("ifsc_code"),
            )
            return out

        return await self._base.fetchrow(sql, *args)

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        q = " ".join(sql.split()).lower()
        if "from payroll_entries pe join users u on u.id = pe.employee_id" in q and "where pe.cycle_id = $1::uuid and pe.status = 'cleared'" in q:
            cid = str(args[0])
            out = []
            for e in self.db.entries.values():
                if e["cycle_id"] != cid or e["status"] != "CLEARED":
                    continue
                u = self.db.users[e["employee_id"]]
                bank = next(
                    (b for b in self.db.bank_details if b["employee_id"] == e["employee_id"] and b["is_primary"]),
                    {},
                )
                row = dict(e)
                row.update(
                    full_name=u["full_name"],
                    email=u["email"],
                    pan_number=u["pan_number"],
                    uan_number=u["uan_number"],
                    payment_method=bank.get("payment_method"),
                    account_holder_name=bank.get("account_holder_name"),
                    bank_name=bank.get("bank_name"),
                    account_number=bank.get("account_number"),
                    ifsc_code=bank.get("ifsc_code"),
                )
                out.append(row)
            return out

        return await self._base.fetch(sql, *args)


@pytest.fixture()
def export_env(monkeypatch):
    db = StatefulPayrollDB()
    base_pool = FakePool(db)

    class WrappedPool:
        def acquire(self):
            class _Ctx:
                async def __aenter__(self_inner):
                    self_inner._cm = base_pool.acquire()
                    raw = await self_inner._cm.__aenter__()
                    return ExporterStatefulConn(raw)

                async def __aexit__(self_inner, exc_type, exc, tb):
                    return await self_inner._cm.__aexit__(exc_type, exc, tb)

            return _Ctx()

    pool = WrappedPool()
    monkeypatch.setattr("app.api.payroll_v2_routes.get_db_pool", lambda: pool)
    monkeypatch.setattr("app.db.get_db_pool", lambda: pool)

    async def _noop():
        return None

    monkeypatch.setattr("app.main.init_db", _noop)
    monkeypatch.setattr("app.main.close_db", _noop)

    actor = {"id": db.hr_id, "email": "hr@aimploy.com", "role": "hr", "permissions": []}

    def set_actor(user_id: str, role: str):
        actor["id"] = user_id
        actor["role"] = role

    app.dependency_overrides[get_current_user] = lambda: dict(actor)
    try:
        with TestClient(app) as client:
            yield client, db, set_actor
    finally:
        app.dependency_overrides.clear()


class TestExporterAPIEndpointsAndRBAC:
    def test_full_export_and_payslip_pdf_download_flow(self, export_env):
        client, db, set_actor = export_env

        # 1. Create & calculate cycle, hold Ajmal
        cid = client.post(
            "/api/v2/payroll/cycles", json={"cycle_month": 11, "cycle_year": 2026}
        ).json()["id"]
        calc = client.post(f"/api/v2/payroll/cycles/{cid}/calculate").json()
        by_emp = {e["employee_id"]: e for e in calc["entries"]}
        darryl_rec = by_emp[db.emp_darryl_id]
        ajmal_rec = by_emp[db.emp_ajmal_id]

        client.patch(
            f"/api/v2/payroll/cycles/{cid}/records/{ajmal_rec['id']}/hold",
            json={"reason": "Hold for audit"},
        )

        # Unapproved DRAFT cycle cannot export ECR / Bank / Form 24Q (409)
        set_actor(db.finance_id, "finance")
        assert client.get(f"/api/v2/payroll/cycles/{cid}/ecr-export").status_code == 409
        assert client.get(f"/api/v2/payroll/cycles/{cid}/bank-export").status_code == 409
        assert client.get(f"/api/v2/payroll/cycles/{cid}/form24q-export").status_code == 409

        # Submit & approve all 3 tiers
        set_actor(str(uuid.uuid4()), "admin")
        client.post(f"/api/v2/payroll/cycles/{cid}/submit")
        client.post(f"/api/v2/payroll/cycles/{cid}/approve", json={"decision": "APPROVED"})
        client.post(f"/api/v2/payroll/cycles/{cid}/approve", json={"decision": "APPROVED"})
        client.post(f"/api/v2/payroll/cycles/{cid}/approve", json={"decision": "APPROVED"})

        # HR and Employee are blocked (403) from financial/compliance exports
        set_actor(db.hr_id, "hr")
        assert client.get(f"/api/v2/payroll/cycles/{cid}/ecr-export").status_code == 403
        assert client.get(f"/api/v2/payroll/cycles/{cid}/bank-export").status_code == 403
        assert client.get(f"/api/v2/payroll/cycles/{cid}/form24q-export").status_code == 403

        set_actor(db.emp_darryl_id, "employee")
        assert client.get(f"/api/v2/payroll/cycles/{cid}/ecr-export").status_code == 403

        # Finance downloads ECR, Bank CSV, Form 24Q -> excludes held Ajmal (only 2 cleared staff)
        set_actor(db.finance_id, "finance")
        ecr_res = client.get(f"/api/v2/payroll/cycles/{cid}/ecr-export")
        assert ecr_res.status_code == 200
        assert ecr_res.headers["x-ecr-member-count"] == "2"
        assert "AJMAL" not in ecr_res.text
        assert "DARRYL M A" in ecr_res.text

        bank_res = client.get(f"/api/v2/payroll/cycles/{cid}/bank-export")
        assert bank_res.status_code == 200
        assert bank_res.headers["x-bank-beneficiary-count"] == "2"
        assert "Ajmal" not in bank_res.text
        assert "Darryl M A" in bank_res.text

        f24_res = client.get(f"/api/v2/payroll/cycles/{cid}/form24q-export")
        assert f24_res.status_code == 200
        assert f24_res.headers["x-form24q-deductees"] == "2"
        assert f24_res.headers["x-form24q-quarter"] == "Q3"
        assert "AJMAL" not in f24_res.text

        # Employee PDF download: Darryl downloads his own PDF cleanly; Ajmal gets 404 on both
        set_actor(db.emp_darryl_id, "employee")
        pdf_res = client.get(f"/api/v2/payroll/me/payslips/{darryl_rec['id']}/pdf")
        assert pdf_res.status_code == 200
        assert pdf_res.headers["content-type"] == "application/pdf"
        reader = PdfReader(io.BytesIO(pdf_res.content), strict=True)
        assert "Darryl M A" in (reader.pages[0].extract_text() or "")

        set_actor(db.emp_ajmal_id, "employee")
        assert client.get(f"/api/v2/payroll/me/payslips/{darryl_rec['id']}/pdf").status_code == 404
        assert client.get(f"/api/v2/payroll/me/payslips/{ajmal_rec['id']}/pdf").status_code == 404

        # Admin/HR record PDF download works for CLEARED records, 404 for ON_HOLD
        set_actor(db.hr_id, "hr")
        assert (
            client.get(
                f"/api/v2/payroll/cycles/{cid}/records/{darryl_rec['id']}/payslip-pdf"
            ).status_code
            == 200
        )
        assert (
            client.get(
                f"/api/v2/payroll/cycles/{cid}/records/{ajmal_rec['id']}/payslip-pdf"
            ).status_code
            == 404
        )
