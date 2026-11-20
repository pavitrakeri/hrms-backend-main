"""
Master Data & Configuration Services for Payroll V2 (India Statutory).

Handles CRUD operations across:
  - `company_statutory_details`
  - `pt_state_slabs` (read + deduplicated lookup)
  - `users` statutory KYC extension fields (`pan_number`, `uan_number`, etc.)
  - `employee_salary_structures` (transactional revisioning)
  - `employee_bank_details` (Indian IFSC validation + primary flag)
  - `employee_tax_declarations` (ESS submission + HR/Finance verification)
  - `employee_salary_advances` (loan EMI creation & status management)
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Optional

from fastapi import HTTPException

from app.datamodels.payroll_v2_models import (
    BankDetailsResponse,
    BankDetailsUpsertRequest,
    CompanyStatutoryResponse,
    CompanyStatutoryUpsertRequest,
    EmployeeStatutoryOverviewRow,
    EmployeeStatutoryProfileUpdate,
    PTSlabItem,
    SalaryAdvanceCreateRequest,
    SalaryAdvanceResponse,
    SalaryAdvanceStatusUpdateRequest,
    SalaryStructureResponse,
    SalaryStructureUpsertRequest,
    TaxDeclarationResponse,
    TaxDeclarationUpsertRequest,
)
from app.features.payroll_v2.adapters.employee_aggregator import (
    get_financial_year_label,
    select_latest_pt_slabs,
)


def validate_uuid(raw_id: str, label: str = "ID") -> str:
    try:
        return str(uuid.UUID(str(raw_id)))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail=f"Invalid UUID for {label}: {raw_id}")


def _iso(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    return str(val)


def _row_to_company(r: Any) -> CompanyStatutoryResponse:
    is_esi = False
    try:
        is_esi = bool(r["is_esic_applicable"])
    except (KeyError, IndexError):
        is_esi = bool(r["esic_establishment_code"] and str(r["esic_establishment_code"]).strip())

    return CompanyStatutoryResponse(
        id=str(r["id"]),
        company_name=r["company_name"],
        tan_number=r["tan_number"],
        pan_number=r["pan_number"],
        epfo_establishment_code=r["epfo_establishment_code"],
        esic_establishment_code=r["esic_establishment_code"],
        is_esic_applicable=is_esi,
        pt_rc_number=r["pt_rc_number"],
        pt_ec_number=r["pt_ec_number"],
        registered_address=r["registered_address"],
        created_at=_iso(r["created_at"]),
        updated_at=_iso(r["updated_at"]),
    )


def _opt_col(r: Any, key: str) -> Any:
    try:
        return r[key]
    except (KeyError, IndexError):
        return None


def _row_to_salary_structure(r: Any) -> SalaryStructureResponse:
    basic = float(r["monthly_basic_pay"])
    hra = float(r["monthly_hra"])
    spec = float(r["monthly_special_allowance"])
    da = float(r["monthly_da"])
    return SalaryStructureResponse(
        id=str(r["id"]),
        employee_id=str(r["employee_id"]),
        employee_name=_opt_col(r, "employee_name"),
        email=_opt_col(r, "email"),
        effective_from=_iso(r["effective_from"]) or "",
        effective_to=_iso(r["effective_to"]),
        is_active=bool(r["is_active"]),
        base_currency=r["base_currency"],
        annual_ctc=float(r["annual_ctc"]),
        monthly_basic_pay=basic,
        monthly_hra=hra,
        monthly_special_allowance=spec,
        monthly_da=da,
        monthly_gross=round(basic + hra + spec + da, 2),
        is_epf_eligible=bool(r["is_epf_eligible"]),
        epf_wage_cap_applied=bool(r["epf_wage_cap_applied"]),
        vpf_percentage=float(r["vpf_percentage"]),
        is_esi_eligible=bool(r["is_esi_eligible"]),
        pt_state_code=r["pt_state_code"],
        tax_regime=r["tax_regime"],
        created_at=_iso(r["created_at"]),
        updated_at=_iso(r["updated_at"]),
    )


def _row_to_bank(r: Any) -> BankDetailsResponse:
    return BankDetailsResponse(
        id=str(r["id"]),
        employee_id=str(r["employee_id"]),
        payment_method=r["payment_method"],
        account_holder_name=r["account_holder_name"],
        bank_name=r["bank_name"],
        account_number=r["account_number"],
        ifsc_code=r["ifsc_code"],
        is_primary=bool(r["is_primary"]),
        created_at=_iso(r["created_at"]),
        updated_at=_iso(r["updated_at"]),
    )


def _row_to_tax_declaration(r: Any) -> TaxDeclarationResponse:
    emp_name = None
    try:
        emp_name = r["employee_name"]
    except (KeyError, IndexError):
        pass
    return TaxDeclarationResponse(
        id=str(r["id"]),
        employee_id=str(r["employee_id"]),
        employee_name=emp_name,
        financial_year=r["financial_year"],
        regime=r["regime"],
        section_80c_amount=float(r["section_80c_amount"]),
        section_80d_amount=float(r["section_80d_amount"]),
        annual_rent_paid=float(r["annual_rent_paid"]),
        home_loan_interest=float(r["home_loan_interest"]),
        nps_80ccd_1b=float(r["nps_80ccd_1b"]),
        is_verified=bool(r["is_verified"]),
        created_at=_iso(r["created_at"]),
        updated_at=_iso(r["updated_at"]),
    )


def _row_to_advance(r: Any) -> SalaryAdvanceResponse:
    emp_name = None
    try:
        emp_name = r["employee_name"]
    except (KeyError, IndexError):
        pass
    return SalaryAdvanceResponse(
        id=str(r["id"]),
        employee_id=str(r["employee_id"]),
        employee_name=emp_name,
        principal_amount=float(r["principal_amount"]),
        monthly_emi_amount=float(r["monthly_emi_amount"]),
        total_installments=int(r["total_installments"]),
        paid_installments=int(r["paid_installments"]),
        remaining_balance=float(r["remaining_balance"]),
        status=r["status"],
        created_at=_iso(r["created_at"]),
        updated_at=_iso(r["updated_at"]),
    )


# ---------------------------------------------------------------------------
# Company Statutory Details & PT Slabs
# ---------------------------------------------------------------------------
async def get_company_statutory_details(conn) -> CompanyStatutoryResponse:
    row = await conn.fetchrow(
        """
        SELECT id, company_name, tan_number, pan_number, epfo_establishment_code,
               esic_establishment_code, is_esic_applicable, pt_rc_number, pt_ec_number, registered_address,
               created_at, updated_at
        FROM company_statutory_details
        ORDER BY updated_at DESC, created_at DESC
        LIMIT 1;
        """
    )
    if not row:
        raise HTTPException(
            status_code=404,
            detail="Company statutory details have not been configured yet.",
        )
    return _row_to_company(row)


async def upsert_company_statutory_details(
    conn, req: CompanyStatutoryUpsertRequest
) -> CompanyStatutoryResponse:
    async with conn.transaction():
        existing = await conn.fetchrow(
            """
            SELECT id
            FROM company_statutory_details
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 1
            FOR UPDATE;
            """
        )
        if existing:
            row = await conn.fetchrow(
                """
                UPDATE company_statutory_details
                SET company_name = $2,
                    tan_number = $3,
                    pan_number = $4,
                    epfo_establishment_code = $5,
                    esic_establishment_code = $6,
                    is_esic_applicable = $7,
                    pt_rc_number = $8,
                    pt_ec_number = $9,
                    registered_address = $10,
                    updated_at = now()
                WHERE id = $1
                RETURNING id, company_name, tan_number, pan_number, epfo_establishment_code,
                          esic_establishment_code, is_esic_applicable, pt_rc_number, pt_ec_number, registered_address,
                          created_at, updated_at;
                """,
                existing["id"],
                req.company_name.strip(),
                req.tan_number,
                req.pan_number,
                req.epfo_establishment_code,
                req.esic_establishment_code.strip() if req.esic_establishment_code else None,
                bool(req.is_esic_applicable),
                req.pt_rc_number.strip() if req.pt_rc_number else None,
                req.pt_ec_number.strip() if req.pt_ec_number else None,
                req.registered_address.strip(),
            )
        else:
            row = await conn.fetchrow(
                """
                INSERT INTO company_statutory_details (
                    company_name, tan_number, pan_number, epfo_establishment_code,
                    esic_establishment_code, is_esic_applicable, pt_rc_number, pt_ec_number, registered_address
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING id, company_name, tan_number, pan_number, epfo_establishment_code,
                          esic_establishment_code, is_esic_applicable, pt_rc_number, pt_ec_number, registered_address,
                          created_at, updated_at;
                """,
                req.company_name.strip(),
                req.tan_number,
                req.pan_number,
                req.epfo_establishment_code,
                req.esic_establishment_code.strip() if req.esic_establishment_code else None,
                bool(req.is_esic_applicable),
                req.pt_rc_number.strip() if req.pt_rc_number else None,
                req.pt_ec_number.strip() if req.pt_ec_number else None,
                req.registered_address.strip(),
            )
    return _row_to_company(row)


async def list_pt_slabs(
    conn, *, as_of_date: Optional[date] = None, state_code: Optional[str] = None
) -> list[PTSlabItem]:
    ref_date = as_of_date or date.today()
    rows = await conn.fetch(
        """
        SELECT id, state_code, state_name, effective_from,
               min_monthly_gross, max_monthly_gross, pt_amount, special_feb_amount
        FROM pt_state_slabs
        WHERE effective_from <= $1
        ORDER BY state_code ASC, effective_from DESC, min_monthly_gross ASC;
        """,
        ref_date,
    )
    slabs = select_latest_pt_slabs(rows)
    if state_code:
        target = state_code.strip().upper()
        slabs = [s for s in slabs if s.state_code == target]

    # Map earliest effective_from per state back cleanly for display
    eff_by_state: dict[str, date] = {}
    for r in rows:
        st = str(r["state_code"]).strip().upper()
        if st not in eff_by_state or r["effective_from"] > eff_by_state[st]:
            eff_by_state[st] = r["effective_from"]

    return [
        PTSlabItem(
            state_code=s.state_code,
            state_name=s.state_name,
            effective_from=eff_by_state.get(s.state_code, ref_date),
            min_monthly_gross=float(s.min_monthly_gross),
            max_monthly_gross=float(s.max_monthly_gross) if s.max_monthly_gross is not None else None,
            pt_amount=float(s.pt_amount),
            special_feb_amount=float(s.special_feb_amount) if s.special_feb_amount is not None else None,
        )
        for s in slabs
    ]


# ---------------------------------------------------------------------------
# Employee Statutory KYC Overview & Profile Update
# ---------------------------------------------------------------------------
async def list_employees_statutory_overview(conn) -> list[EmployeeStatutoryOverviewRow]:
    rows = await conn.fetch(
        """
        SELECT
            u.id::text AS employee_id,
            u.full_name,
            u.email,
            d.name AS department,
            u.designation,
            u.joining_date,
            u.date_of_exit,
            u.pan_number,
            u.uan_number,
            u.aadhaar_number,
            u.esic_ip_number,
            u.father_or_spouse_name,
            u.exit_reason_code,
            ess.id AS structure_id,
            ess.annual_ctc,
            ess.is_epf_eligible,
            ess.is_esi_eligible,
            ess.pt_state_code,
            ess.tax_regime,
            ebd.id AS bank_id,
            ebd.payment_method,
            ebd.bank_name,
            ebd.ifsc_code
        FROM users u
        LEFT JOIN departments d ON d.id = u.department_id
        LEFT JOIN employee_salary_structures ess
               ON ess.employee_id = u.id AND ess.is_active = true
        LEFT JOIN employee_bank_details ebd
               ON ebd.employee_id = u.id AND ebd.is_primary = true
        WHERE u.is_active = true
        ORDER BY u.full_name ASC;
        """
    )
    results: list[EmployeeStatutoryOverviewRow] = []
    for r in rows:
        warnings: list[str] = []
        has_struct = r["structure_id"] is not None
        has_bank = r["bank_id"] is not None
        if not has_struct:
            warnings.append("Missing active salary structure")
        if not r["pan_number"]:
            warnings.append("Missing PAN")
        if has_struct and bool(r["is_epf_eligible"]) and not r["uan_number"]:
            warnings.append("Missing 12-digit UAN")
        if has_struct and bool(r["is_esi_eligible"]) and not r["esic_ip_number"]:
            warnings.append("Missing ESIC IP Number")
        if not has_bank:
            warnings.append("Missing primary bank details")
        elif r["payment_method"] == "BANK_TRANSFER" and len((r["ifsc_code"] or "").strip()) != 11:
            warnings.append("Invalid IFSC code")

        results.append(
            EmployeeStatutoryOverviewRow(
                employee_id=r["employee_id"],
                full_name=r["full_name"],
                email=r["email"],
                department=r["department"],
                designation=r["designation"],
                joining_date=_iso(r["joining_date"]),
                date_of_exit=_iso(r["date_of_exit"]),
                pan_number=r["pan_number"],
                uan_number=r["uan_number"],
                aadhaar_number=r["aadhaar_number"],
                esic_ip_number=r["esic_ip_number"],
                father_or_spouse_name=r["father_or_spouse_name"],
                exit_reason_code=r["exit_reason_code"],
                has_active_structure=has_struct,
                annual_ctc=float(r["annual_ctc"]) if r["annual_ctc"] is not None else None,
                pt_state_code=r["pt_state_code"],
                tax_regime=r["tax_regime"],
                has_primary_bank=has_bank,
                bank_name=r["bank_name"],
                ifsc_code=r["ifsc_code"],
                kyc_warnings=warnings,
            )
        )
    return results


async def update_employee_statutory_profile(
    conn, employee_id: str, req: EmployeeStatutoryProfileUpdate
) -> dict[str, Any]:
    emp_uuid = validate_uuid(employee_id, "employee_id")
    updated = await conn.fetchrow(
        """
        UPDATE users
        SET pan_number = COALESCE($2, pan_number),
            uan_number = COALESCE($3, uan_number),
            aadhaar_number = COALESCE($4, aadhaar_number),
            esic_ip_number = COALESCE($5, esic_ip_number),
            father_or_spouse_name = COALESCE($6, father_or_spouse_name),
            date_of_exit = $7,
            exit_reason_code = $8
        WHERE id = $1::uuid
        RETURNING id::text AS employee_id, full_name, pan_number, uan_number,
                  aadhaar_number, esic_ip_number, father_or_spouse_name,
                  date_of_exit, exit_reason_code;
        """,
        emp_uuid,
        req.pan_number,
        req.uan_number,
        req.aadhaar_number,
        req.esic_ip_number,
        req.father_or_spouse_name.strip() if req.father_or_spouse_name else None,
        req.date_of_exit,
        req.exit_reason_code.strip() if req.exit_reason_code else None,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Employee not found.")
    return {
        "employee_id": updated["employee_id"],
        "full_name": updated["full_name"],
        "pan_number": updated["pan_number"],
        "uan_number": updated["uan_number"],
        "aadhaar_number": updated["aadhaar_number"],
        "esic_ip_number": updated["esic_ip_number"],
        "father_or_spouse_name": updated["father_or_spouse_name"],
        "date_of_exit": _iso(updated["date_of_exit"]),
        "exit_reason_code": updated["exit_reason_code"],
    }


# ---------------------------------------------------------------------------
# Employee Salary Structures
# ---------------------------------------------------------------------------
async def list_salary_structures(
    conn,
    *,
    employee_id: Optional[str] = None,
    include_history: bool = False,
) -> list[SalaryStructureResponse]:
    conditions: list[str] = []
    params: list[Any] = []
    idx = 1

    if employee_id:
        emp_uuid = validate_uuid(employee_id, "employee_id")
        conditions.append(f"ess.employee_id = ${idx}::uuid")
        params.append(emp_uuid)
        idx += 1

    if not include_history:
        conditions.append("ess.is_active = true")

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = await conn.fetch(
        f"""
        SELECT ess.id, ess.employee_id, u.full_name AS employee_name, u.email,
               ess.effective_from, ess.effective_to, ess.is_active,
               ess.base_currency, ess.annual_ctc, ess.monthly_basic_pay,
               ess.monthly_hra, ess.monthly_special_allowance, ess.monthly_da,
               ess.is_epf_eligible, ess.epf_wage_cap_applied, ess.vpf_percentage,
               ess.is_esi_eligible, ess.pt_state_code, ess.tax_regime,
               ess.created_at, ess.updated_at
        FROM employee_salary_structures ess
        JOIN users u ON u.id = ess.employee_id
        {where_clause}
        ORDER BY u.full_name ASC, ess.effective_from DESC, ess.created_at DESC;
        """,
        *params,
    )
    return [_row_to_salary_structure(r) for r in rows]


async def upsert_salary_structure(
    conn, req: SalaryStructureUpsertRequest
) -> SalaryStructureResponse:
    emp_uuid = validate_uuid(req.employee_id, "employee_id")

    async with conn.transaction():
        emp = await conn.fetchrow(
            "SELECT id, full_name, email FROM users WHERE id = $1::uuid AND is_active = true;",
            emp_uuid,
        )
        if not emp:
            raise HTTPException(status_code=404, detail="Active employee not found.")

        close_date = req.effective_from - timedelta(days=1)
        await conn.execute(
            """
            UPDATE employee_salary_structures
            SET is_active = false,
                effective_to = COALESCE(effective_to, GREATEST(effective_from, $2::date)),
                updated_at = now()
            WHERE employee_id = $1::uuid
              AND is_active = true;
            """,
            emp_uuid,
            close_date,
        )

        row = await conn.fetchrow(
            """
            INSERT INTO employee_salary_structures (
                employee_id, effective_from, is_active, base_currency,
                annual_ctc, monthly_basic_pay, monthly_hra, monthly_special_allowance,
                monthly_da, is_epf_eligible, epf_wage_cap_applied, vpf_percentage,
                is_esi_eligible, pt_state_code, tax_regime
            )
            VALUES (
                $1::uuid, $2, true, $3,
                $4, $5, $6, $7,
                $8, $9, $10, $11,
                $12, $13, $14
            )
            RETURNING id, employee_id, effective_from, effective_to, is_active,
                      base_currency, annual_ctc, monthly_basic_pay, monthly_hra,
                      monthly_special_allowance, monthly_da, is_epf_eligible,
                      epf_wage_cap_applied, vpf_percentage, is_esi_eligible,
                      pt_state_code, tax_regime, created_at, updated_at;
            """,
            emp_uuid,
            req.effective_from,
            req.base_currency,
            req.annual_ctc,
            req.monthly_basic_pay,
            req.monthly_hra,
            req.monthly_special_allowance,
            req.monthly_da,
            req.is_epf_eligible,
            req.epf_wage_cap_applied,
            req.vpf_percentage,
            bool(req.is_esi_eligible),
            req.pt_state_code,
            req.tax_regime,
        )

    enriched = dict(row)
    enriched["employee_name"] = emp["full_name"]
    enriched["email"] = emp["email"]
    return _row_to_salary_structure(enriched)


# ---------------------------------------------------------------------------
# Employee Bank Details
# ---------------------------------------------------------------------------
async def get_employee_bank_details(conn, employee_id: str) -> BankDetailsResponse:
    emp_uuid = validate_uuid(employee_id, "employee_id")
    row = await conn.fetchrow(
        """
        SELECT id, employee_id, payment_method, account_holder_name, bank_name,
               account_number, ifsc_code, is_primary, created_at, updated_at
        FROM employee_bank_details
        WHERE employee_id = $1::uuid
        ORDER BY is_primary DESC, updated_at DESC, created_at DESC
        LIMIT 1;
        """,
        emp_uuid,
    )
    if not row:
        raise HTTPException(
            status_code=404, detail="Bank details not found for employee."
        )
    return _row_to_bank(row)


async def upsert_employee_bank_details(
    conn, employee_id: str, req: BankDetailsUpsertRequest
) -> BankDetailsResponse:
    emp_uuid = validate_uuid(employee_id, "employee_id")

    async with conn.transaction():
        exists = await conn.fetchval(
            "SELECT 1 FROM users WHERE id = $1::uuid;", emp_uuid
        )
        if not exists:
            raise HTTPException(status_code=404, detail="Employee not found.")

        if req.is_primary:
            await conn.execute(
                """
                UPDATE employee_bank_details
                SET is_primary = false,
                    updated_at = now()
                WHERE employee_id = $1::uuid
                  AND is_primary = true;
                """,
                emp_uuid,
            )

        row = await conn.fetchrow(
            """
            INSERT INTO employee_bank_details (
                employee_id, payment_method, account_holder_name, bank_name,
                account_number, ifsc_code, is_primary
            )
            VALUES ($1::uuid, $2, $3, $4, $5, $6, $7)
            RETURNING id, employee_id, payment_method, account_holder_name, bank_name,
                      account_number, ifsc_code, is_primary, created_at, updated_at;
            """,
            emp_uuid,
            req.payment_method,
            req.account_holder_name.strip(),
            req.bank_name.strip(),
            req.account_number,
            req.ifsc_code,
            req.is_primary,
        )
    return _row_to_bank(row)


# ---------------------------------------------------------------------------
# Employee Tax Declarations
# ---------------------------------------------------------------------------
async def list_tax_declarations(
    conn, *, financial_year: Optional[str] = None
) -> list[TaxDeclarationResponse]:
    params: list[Any] = []
    where = ""
    if financial_year:
        where = "WHERE etd.financial_year = $1"
        params.append(financial_year.strip())

    rows = await conn.fetch(
        f"""
        SELECT etd.id, etd.employee_id, u.full_name AS employee_name,
               etd.financial_year, etd.regime, etd.section_80c_amount,
               etd.section_80d_amount, etd.annual_rent_paid,
               etd.home_loan_interest, etd.nps_80ccd_1b, etd.is_verified,
               etd.created_at, etd.updated_at
        FROM employee_tax_declarations etd
        JOIN users u ON u.id = etd.employee_id
        {where}
        ORDER BY etd.financial_year DESC, u.full_name ASC;
        """,
        *params,
    )
    return [_row_to_tax_declaration(r) for r in rows]


async def get_employee_tax_declaration(
    conn, employee_id: str, *, financial_year: Optional[str] = None
) -> Optional[TaxDeclarationResponse]:
    emp_uuid = validate_uuid(employee_id, "employee_id")
    if financial_year is None:
        today = date.today()
        financial_year, _, _ = get_financial_year_label(today.month, today.year)

    row = await conn.fetchrow(
        """
        SELECT etd.id, etd.employee_id, u.full_name AS employee_name,
               etd.financial_year, etd.regime, etd.section_80c_amount,
               etd.section_80d_amount, etd.annual_rent_paid,
               etd.home_loan_interest, etd.nps_80ccd_1b, etd.is_verified,
               etd.created_at, etd.updated_at
        FROM employee_tax_declarations etd
        JOIN users u ON u.id = etd.employee_id
        WHERE etd.employee_id = $1::uuid
          AND etd.financial_year = $2;
        """,
        emp_uuid,
        financial_year,
    )
    return _row_to_tax_declaration(row) if row else None


async def upsert_employee_tax_declaration(
    conn, employee_id: str, req: TaxDeclarationUpsertRequest
) -> TaxDeclarationResponse:
    emp_uuid = validate_uuid(employee_id, "employee_id")
    row = await conn.fetchrow(
        """
        INSERT INTO employee_tax_declarations (
            employee_id, financial_year, regime, section_80c_amount,
            section_80d_amount, annual_rent_paid, home_loan_interest,
            nps_80ccd_1b, is_verified
        )
        VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, false)
        ON CONFLICT (employee_id, financial_year)
        DO UPDATE SET
            regime = EXCLUDED.regime,
            section_80c_amount = EXCLUDED.section_80c_amount,
            section_80d_amount = EXCLUDED.section_80d_amount,
            annual_rent_paid = EXCLUDED.annual_rent_paid,
            home_loan_interest = EXCLUDED.home_loan_interest,
            nps_80ccd_1b = EXCLUDED.nps_80ccd_1b,
            is_verified = false,
            updated_at = now()
        RETURNING id, employee_id, financial_year, regime, section_80c_amount,
                  section_80d_amount, annual_rent_paid, home_loan_interest,
                  nps_80ccd_1b, is_verified, created_at, updated_at;
        """,
        emp_uuid,
        req.financial_year,
        req.regime,
        req.section_80c_amount,
        req.section_80d_amount,
        req.annual_rent_paid,
        req.home_loan_interest,
        req.nps_80ccd_1b,
    )
    return _row_to_tax_declaration(row)


async def verify_tax_declaration(
    conn, declaration_id: str, is_verified: bool
) -> TaxDeclarationResponse:
    decl_uuid = validate_uuid(declaration_id, "declaration_id")
    row = await conn.fetchrow(
        """
        UPDATE employee_tax_declarations
        SET is_verified = $2,
            updated_at = now()
        WHERE id = $1::uuid
        RETURNING id, employee_id, financial_year, regime, section_80c_amount,
                  section_80d_amount, annual_rent_paid, home_loan_interest,
                  nps_80ccd_1b, is_verified, created_at, updated_at;
        """,
        decl_uuid,
        is_verified,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Tax declaration not found.")
    return _row_to_tax_declaration(row)


# ---------------------------------------------------------------------------
# Employee Salary Advances & EMI Recovery Schedules
# ---------------------------------------------------------------------------
async def list_salary_advances(
    conn,
    *,
    employee_id: Optional[str] = None,
    status: Optional[str] = None,
) -> list[SalaryAdvanceResponse]:
    conditions: list[str] = []
    params: list[Any] = []
    idx = 1

    if employee_id:
        emp_uuid = validate_uuid(employee_id, "employee_id")
        conditions.append(f"esa.employee_id = ${idx}::uuid")
        params.append(emp_uuid)
        idx += 1

    if status:
        conditions.append(f"esa.status = ${idx}")
        params.append(status.strip().upper())
        idx += 1

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = await conn.fetch(
        f"""
        SELECT esa.id, esa.employee_id, u.full_name AS employee_name,
               esa.principal_amount, esa.monthly_emi_amount,
               esa.total_installments, esa.paid_installments,
               esa.remaining_balance, esa.status,
               esa.created_at, esa.updated_at
        FROM employee_salary_advances esa
        JOIN users u ON u.id = esa.employee_id
        {where}
        ORDER BY esa.created_at DESC;
        """,
        *params,
    )
    return [_row_to_advance(r) for r in rows]


async def create_salary_advance(
    conn, req: SalaryAdvanceCreateRequest
) -> SalaryAdvanceResponse:
    emp_uuid = validate_uuid(req.employee_id, "employee_id")
    emp = await conn.fetchrow(
        "SELECT id, full_name FROM users WHERE id = $1::uuid AND is_active = true;",
        emp_uuid,
    )
    if not emp:
        raise HTTPException(status_code=404, detail="Active employee not found.")

    row = await conn.fetchrow(
        """
        INSERT INTO employee_salary_advances (
            employee_id, principal_amount, monthly_emi_amount,
            total_installments, paid_installments, remaining_balance, status
        )
        VALUES ($1::uuid, $2, $3, $4, 0, $2, 'ACTIVE')
        RETURNING id, employee_id, principal_amount, monthly_emi_amount,
                  total_installments, paid_installments, remaining_balance,
                  status, created_at, updated_at;
        """,
        emp_uuid,
        req.principal_amount,
        req.monthly_emi_amount,
        req.total_installments,
    )
    enriched = dict(row)
    enriched["employee_name"] = emp["full_name"]
    return _row_to_advance(enriched)


async def update_salary_advance_status(
    conn, advance_id: str, req: SalaryAdvanceStatusUpdateRequest
) -> SalaryAdvanceResponse:
    adv_uuid = validate_uuid(advance_id, "advance_id")
    existing = await conn.fetchrow(
        "SELECT status FROM employee_salary_advances WHERE id = $1::uuid;",
        adv_uuid,
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Salary advance not found.")
    if existing["status"] == "COMPLETED":
        raise HTTPException(
            status_code=409,
            detail="Cannot modify status of an already COMPLETED salary advance.",
        )

    row = await conn.fetchrow(
        """
        UPDATE employee_salary_advances
        SET status = $2,
            updated_at = now()
        WHERE id = $1::uuid
        RETURNING id, employee_id, principal_amount, monthly_emi_amount,
                  total_installments, paid_installments, remaining_balance,
                  status, created_at, updated_at;
        """,
        adv_uuid,
        req.status,
    )
    return _row_to_advance(row)
