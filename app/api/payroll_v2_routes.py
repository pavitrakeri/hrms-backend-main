"""
FastAPI Router for Payroll V2 (Indian Statutory Payroll Engine).

Prefix: /api/v2/payroll
All database access runs through `app.db.get_db_pool()` and explicit RBAC guards.
Phase 5 will append /cycles/{cycle_id}/ecr-export, /bank-export, /form24q-export,
and /payslips/{entry_id}/pdf routes to this router.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response

from app.datamodels.payroll_v2_models import (
    BankBatchResponse,
    BankBatchSubmissionRequest,
    BankDetailsResponse,
    BankDetailsUpsertRequest,
    BankReconciliationSummaryResponse,
    CompanyStatutoryResponse,
    CompanyStatutoryUpsertRequest,
    CycleActionStatusResponse,
    CycleApprovalActionRequest,
    CycleCalculateRequest,
    CycleCreateRequest,
    CycleMarkDisbursedRequest,
    EmployeePayslipDetailResponse,
    EmployeePayslipListItem,
    EmployeeStatutoryOverviewRow,
    EmployeeStatutoryProfileUpdate,
    EntryHoldRequest,
    EntryReleaseRequest,
    OffCycleInitiateRequest,
    PTSlabItem,
    PayrollAuditLogRow,
    PayrollCycleDetailResponse,
    PayrollCycleSummary,
    PayrollDocumentMetadata,
    ProvisionalPreviewResponse,
    RemediateFailureRequest,
    SalaryAdvanceCreateRequest,
    SalaryAdvanceResponse,
    SalaryAdvanceStatusUpdateRequest,
    SalaryStructureResponse,
    SalaryStructureUpsertRequest,
    StatutoryCategory,
    StatutoryRemittanceRow,
    StatutoryRemittanceUpdateRequest,
    TaxDeclarationResponse,
    TaxDeclarationUpsertRequest,
    TaxDeclarationVerifyRequest,
)
from app.db import get_db_pool
from app.features.auth import get_current_user
from app.features.payroll_v2 import (
    bank_reconciliation,
    cycle_service,
    document_vault,
    master_service,
    remediation_service,
    statutory_remittance,
)
from app.features.payroll_v2.exporters import (
    generate_cycle_bank_export,
    generate_cycle_ecr_export,
    generate_cycle_form24q_export,
    generate_payslip_pdf_for_employee,
)
from app.features.payroll_v2.rbac import (
    require_payroll_roles,
    require_self_or_roles,
)

router = APIRouter(prefix="/api/v2/payroll", tags=["Payroll V2 (India Statutory)"])


# ---------------------------------------------------------------------------
# 1. Corporate & Statutory Masters
# ---------------------------------------------------------------------------
@router.get("/company-statutory", response_model=CompanyStatutoryResponse)
async def get_company_statutory_api(user=Depends(get_current_user)):
    require_payroll_roles(user, {"hr", "finance", "cfo", "admin"}, allow_manage_payroll_perm=True)
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await master_service.get_company_statutory_details(conn)


@router.put("/company-statutory", response_model=CompanyStatutoryResponse)
async def upsert_company_statutory_api(
    req: CompanyStatutoryUpsertRequest,
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"hr", "admin"}, allow_manage_payroll_perm=True)
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await master_service.upsert_company_statutory_details(conn, req)


@router.get("/pt-slabs", response_model=list[PTSlabItem])
async def list_pt_slabs_api(
    state_code: Optional[str] = Query(default=None),
    as_of_date: Optional[date] = Query(default=None),
    user=Depends(get_current_user),
):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await master_service.list_pt_slabs(
            conn, as_of_date=as_of_date, state_code=state_code
        )


@router.get("/employees/statutory-overview", response_model=list[EmployeeStatutoryOverviewRow])
async def list_employees_statutory_overview_api(user=Depends(get_current_user)):
    require_payroll_roles(user, {"hr", "finance", "cfo", "admin"}, allow_manage_payroll_perm=True)
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await master_service.list_employees_statutory_overview(conn)


@router.put("/employees/{employee_id}/statutory-profile")
async def update_employee_statutory_profile_api(
    employee_id: str,
    req: EmployeeStatutoryProfileUpdate,
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"hr", "admin"}, allow_manage_payroll_perm=True)
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await master_service.update_employee_statutory_profile(
            conn, employee_id, req
        )


@router.get("/salary-structures", response_model=list[SalaryStructureResponse])
async def list_salary_structures_api(
    employee_id: Optional[str] = Query(default=None),
    include_history: bool = Query(default=False),
    user=Depends(get_current_user),
):
    if employee_id:
        require_self_or_roles(user, employee_id, {"hr", "finance", "cfo", "admin"})
    else:
        require_payroll_roles(user, {"hr", "finance", "cfo", "admin"}, allow_manage_payroll_perm=True)
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await master_service.list_salary_structures(
            conn, employee_id=employee_id, include_history=include_history
        )


@router.post("/salary-structures", response_model=SalaryStructureResponse, status_code=201)
async def upsert_salary_structure_api(
    req: SalaryStructureUpsertRequest,
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"hr", "admin"}, allow_manage_payroll_perm=True)
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await master_service.upsert_salary_structure(conn, req)


@router.get("/bank-details/{employee_id}", response_model=BankDetailsResponse)
async def get_employee_bank_details_api(
    employee_id: str,
    user=Depends(get_current_user),
):
    require_self_or_roles(user, employee_id, {"hr", "finance", "cfo", "admin"})
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await master_service.get_employee_bank_details(conn, employee_id)


@router.put("/bank-details/{employee_id}", response_model=BankDetailsResponse)
async def upsert_employee_bank_details_api(
    employee_id: str,
    req: BankDetailsUpsertRequest,
    user=Depends(get_current_user),
):
    require_self_or_roles(user, employee_id, {"hr", "admin"})
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await master_service.upsert_employee_bank_details(conn, employee_id, req)


@router.get("/tax-declarations", response_model=list[TaxDeclarationResponse])
async def list_tax_declarations_api(
    financial_year: Optional[str] = Query(default=None),
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"hr", "finance", "cfo", "admin"}, allow_manage_payroll_perm=True)
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await master_service.list_tax_declarations(
            conn, financial_year=financial_year
        )


@router.patch("/tax-declarations/{declaration_id}/verify", response_model=TaxDeclarationResponse)
async def verify_tax_declaration_api(
    declaration_id: str,
    req: TaxDeclarationVerifyRequest,
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"hr", "finance", "admin"}, allow_manage_payroll_perm=True)
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await master_service.verify_tax_declaration(
            conn, declaration_id, req.is_verified
        )


@router.get("/advances", response_model=list[SalaryAdvanceResponse])
async def list_salary_advances_api(
    employee_id: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"hr", "finance", "cfo", "admin"}, allow_manage_payroll_perm=True)
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await master_service.list_salary_advances(
            conn, employee_id=employee_id, status=status
        )


@router.post("/advances", response_model=SalaryAdvanceResponse, status_code=201)
async def create_salary_advance_api(
    req: SalaryAdvanceCreateRequest,
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"hr", "finance", "admin"}, allow_manage_payroll_perm=True)
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await master_service.create_salary_advance(conn, req)


@router.patch("/advances/{advance_id}/status", response_model=SalaryAdvanceResponse)
async def update_salary_advance_status_api(
    advance_id: str,
    req: SalaryAdvanceStatusUpdateRequest,
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"hr", "finance", "admin"}, allow_manage_payroll_perm=True)
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await master_service.update_salary_advance_status(conn, advance_id, req)


# ---------------------------------------------------------------------------
# 2. Payroll Cycle Workflow & Line-Item Hold State Machine
# ---------------------------------------------------------------------------
@router.get("/cycles", response_model=list[PayrollCycleSummary])
async def list_cycles_api(
    cycle_year: Optional[int] = Query(default=None),
    status: Optional[str] = Query(default=None),
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"hr", "finance", "cfo", "admin"}, allow_manage_payroll_perm=True)
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await cycle_service.list_cycles(conn, cycle_year=cycle_year, status=status)


@router.post("/cycles", response_model=PayrollCycleSummary, status_code=201)
async def create_cycle_api(
    req: CycleCreateRequest,
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"hr", "admin"}, allow_manage_payroll_perm=True)
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await cycle_service.create_cycle(conn, user, req)


@router.post("/cycles/{cycle_id}/calculate", response_model=PayrollCycleDetailResponse)
async def calculate_cycle_api(
    cycle_id: str,
    req: Optional[CycleCalculateRequest] = Body(default=None),
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"hr", "admin"}, allow_manage_payroll_perm=True)
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await cycle_service.calculate_cycle(conn, cycle_id, req)


@router.get("/cycles/{cycle_id}", response_model=PayrollCycleDetailResponse)
async def get_cycle_detail_api(
    cycle_id: str,
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"hr", "finance", "cfo", "admin"}, allow_manage_payroll_perm=True)
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await cycle_service.get_cycle_detail(conn, cycle_id)


@router.patch("/cycles/{cycle_id}/records/{record_id}/hold", response_model=PayrollCycleDetailResponse)
async def hold_entry_api(
    cycle_id: str,
    record_id: str,
    req: EntryHoldRequest,
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"hr", "finance", "admin"}, allow_manage_payroll_perm=True)
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await cycle_service.hold_entry(conn, cycle_id, record_id, req)


@router.patch("/cycles/{cycle_id}/records/{record_id}/release", response_model=PayrollCycleDetailResponse)
async def release_entry_api(
    cycle_id: str,
    record_id: str,
    req: Optional[EntryReleaseRequest] = Body(default=None),
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"hr", "finance", "admin"}, allow_manage_payroll_perm=True)
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await cycle_service.release_entry(conn, cycle_id, record_id, req)


@router.post("/cycles/{cycle_id}/records/{record_id}/recalculate", response_model=PayrollCycleDetailResponse)
async def recalculate_entry_api(
    cycle_id: str,
    record_id: str,
    strict_attendance: bool = Query(default=True),
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"hr", "admin"}, allow_manage_payroll_perm=True)
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await cycle_service.recalculate_single_entry(
            conn, cycle_id, record_id, strict_attendance=strict_attendance
        )


@router.post("/cycles/{cycle_id}/submit", response_model=PayrollCycleDetailResponse)
async def submit_cycle_api(
    cycle_id: str,
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"hr", "admin"}, allow_manage_payroll_perm=True)
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await cycle_service.submit_cycle_for_review(conn, cycle_id)


@router.post("/cycles/{cycle_id}/approve", response_model=PayrollCycleDetailResponse)
async def approve_cycle_api(
    cycle_id: str,
    req: CycleApprovalActionRequest,
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"hr", "finance", "cfo", "admin"}, allow_manage_payroll_perm=True)
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await cycle_service.action_cycle_approval(conn, user, cycle_id, req)


@router.post("/cycles/{cycle_id}/mark-disbursed", response_model=CycleActionStatusResponse)
async def mark_cycle_disbursed_api(
    cycle_id: str,
    req: CycleMarkDisbursedRequest,
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"cfo", "admin"})
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await cycle_service.mark_cycle_disbursed(conn, cycle_id, req)


# ---------------------------------------------------------------------------
# 2B. Statutory Compliance & Banking File Exporters (Phase 5)
# ---------------------------------------------------------------------------
@router.get("/cycles/{cycle_id}/ecr-export")
async def export_cycle_ecr_api(
    cycle_id: str,
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"finance", "cfo", "admin"})
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        bundle = await generate_cycle_ecr_export(conn, cycle_id)
    return Response(
        content=bundle.content,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{bundle.filename}"',
            "X-ECR-Member-Count": str(bundle.member_count),
            "X-ECR-Total-EE": str(bundle.total_ee_share),
            "X-ECR-Total-EPS": str(bundle.total_eps_share),
            "X-ECR-Total-ER": str(bundle.total_er_share),
        },
    )


@router.get("/cycles/{cycle_id}/bank-export")
async def export_cycle_bank_csv_api(
    cycle_id: str,
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"finance", "cfo", "admin"})
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        bundle = await generate_cycle_bank_export(conn, cycle_id)
    return Response(
        content=bundle.content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{bundle.filename}"',
            "X-Bank-Beneficiary-Count": str(bundle.beneficiary_count),
            "X-Bank-Total-Amount": f"{bundle.total_payout_amount:.2f}",
        },
    )


@router.get("/cycles/{cycle_id}/form24q-export")
async def export_cycle_form24q_api(
    cycle_id: str,
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"finance", "cfo", "admin"})
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        bundle = await generate_cycle_form24q_export(conn, cycle_id)
    return Response(
        content=bundle.content,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{bundle.filename}"',
            "X-Form24Q-Quarter": bundle.quarter,
            "X-Form24Q-FY": bundle.financial_year,
            "X-Form24Q-Deductees": str(bundle.deductee_count),
            "X-Form24Q-Total-TDS": f"{bundle.total_tds_deducted:.2f}",
        },
    )


@router.get("/cycles/{cycle_id}/records/{record_id}/payslip-pdf")
async def download_record_payslip_pdf_api(
    cycle_id: str,
    record_id: str,
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"hr", "finance", "cfo", "admin"}, allow_manage_payroll_perm=True)
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        bundle = await generate_payslip_pdf_for_employee(
            conn, entry_id=record_id, cycle_id=cycle_id
        )
    return Response(
        content=bundle.pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{bundle.filename}"',
        },
    )


# ---------------------------------------------------------------------------
# 3. Employee Self-Service (ESS)
# ---------------------------------------------------------------------------
@router.get("/me/payslips", response_model=list[EmployeePayslipListItem])
async def list_my_payslips_api(user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await cycle_service.list_my_payslips(conn, user["id"])


@router.get("/me/payslips/{entry_id}", response_model=EmployeePayslipDetailResponse)
async def get_my_payslip_detail_api(
    entry_id: str,
    user=Depends(get_current_user),
):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await cycle_service.get_my_payslip_detail(conn, user["id"], entry_id)


@router.get("/me/payslips/{entry_id}/pdf")
async def download_my_payslip_pdf_api(
    entry_id: str,
    user=Depends(get_current_user),
):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        bundle = await generate_payslip_pdf_for_employee(
            conn, entry_id=entry_id, requester_employee_id=user["id"]
        )
    return Response(
        content=bundle.pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{bundle.filename}"',
        },
    )


@router.get("/me/provisional-preview", response_model=ProvisionalPreviewResponse)
async def get_my_provisional_preview_api(
    month: Optional[int] = Query(default=None, ge=1, le=12),
    year: Optional[int] = Query(default=None, ge=2020, le=2100),
    as_of_date: Optional[date] = Query(default=None),
    user=Depends(get_current_user),
):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await cycle_service.get_my_provisional_preview(
            conn, user["id"], month=month, year=year, as_of_date=as_of_date
        )


@router.get("/me/tax-declarations", response_model=Optional[TaxDeclarationResponse])
async def get_my_tax_declaration_api(
    financial_year: Optional[str] = Query(default=None),
    user=Depends(get_current_user),
):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await master_service.get_employee_tax_declaration(
            conn, user["id"], financial_year=financial_year
        )


@router.put("/me/tax-declarations", response_model=TaxDeclarationResponse)
async def upsert_my_tax_declaration_api(
    req: TaxDeclarationUpsertRequest,
    user=Depends(get_current_user),
):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await master_service.upsert_employee_tax_declaration(
            conn, user["id"], req
        )


@router.get("/me/advances", response_model=list[SalaryAdvanceResponse])
async def list_my_advances_api(user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await master_service.list_salary_advances(conn, employee_id=user["id"])


@router.get("/me/salary-structure", response_model=list[SalaryStructureResponse])
async def get_my_salary_structure_api(user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await master_service.list_salary_structures(
            conn, employee_id=user["id"], include_history=False
        )


# ---------------------------------------------------------------------------
# 4. Bank Disbursement, Batches & Reverse MIS Reconciliation
# ---------------------------------------------------------------------------
@router.post(
    "/cycles/{cycle_id}/disbursement/record-submission",
    response_model=BankBatchResponse,
)
async def record_bank_submission_api(
    cycle_id: str,
    req: BankBatchSubmissionRequest,
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"cfo", "finance", "admin"})
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await bank_reconciliation.record_bank_submission(
            conn, user, cycle_id, req
        )


@router.post(
    "/cycles/{cycle_id}/disbursement/reconcile-mis",
    response_model=BankReconciliationSummaryResponse,
)
async def reconcile_bank_mis_api(
    cycle_id: str,
    file: UploadFile = File(...),
    batch_reference: Optional[str] = Form(None),
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"cfo", "finance", "admin"})
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await bank_reconciliation.reconcile_bank_mis_file(
            conn, user, cycle_id, file, batch_reference=batch_reference
        )


@router.get(
    "/cycles/{cycle_id}/disbursement/reconciliation-summary",
    response_model=BankReconciliationSummaryResponse,
)
async def get_reconciliation_summary_api(
    cycle_id: str,
    reconciliation_id: Optional[str] = Query(default=None),
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"cfo", "finance", "admin", "hr"}, allow_manage_payroll_perm=True)
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await bank_reconciliation.get_reconciliation_summary(
            conn, reconciliation_id=reconciliation_id, cycle_id=cycle_id
        )


# ---------------------------------------------------------------------------
# 5. Failed Payment Remediation & Off-Cycle Settlement
# ---------------------------------------------------------------------------
@router.post(
    "/cycles/{cycle_id}/records/{record_id}/remediate-failure",
)
async def remediate_failure_api(
    cycle_id: str,
    record_id: str,
    req: RemediateFailureRequest,
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"cfo", "finance", "admin"})
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await remediation_service.update_entry_remediation(
            conn, user, cycle_id, record_id, req
        )


@router.post(
    "/cycles/{cycle_id}/disbursement/initiate-off-cycle",
    response_model=PayrollCycleSummary,
)
async def initiate_off_cycle_api(
    cycle_id: str,
    req: OffCycleInitiateRequest,
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"cfo", "finance", "admin"})
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await remediation_service.initiate_off_cycle_settlement(
            conn, user, cycle_id, req
        )


@router.get("/audit-logs", response_model=list[PayrollAuditLogRow])
async def list_audit_logs_api(
    cycle_id: Optional[str] = Query(default=None),
    entry_id: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"cfo", "finance", "admin", "hr"}, allow_manage_payroll_perm=True)
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await remediation_service.list_payroll_audit_logs(
            conn, cycle_id=cycle_id, entry_id=entry_id, limit=limit
        )


# ---------------------------------------------------------------------------
# 6. Statutory Remittance & Compliance Tracking (EPFO, ESIC, PT, TDS)
# ---------------------------------------------------------------------------
@router.get(
    "/cycles/{cycle_id}/statutory-remittances",
    response_model=list[StatutoryRemittanceRow],
)
async def list_statutory_remittances_api(
    cycle_id: str,
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"cfo", "finance", "admin", "hr"}, allow_manage_payroll_perm=True)
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        rows = await statutory_remittance.list_statutory_remittances_for_cycle(
            conn, cycle_id
        )
        if not rows:
            # Auto-initialize if first time opening
            rows = await statutory_remittance.init_statutory_remittances_for_cycle(
                conn, cycle_id
            )
        return rows


@router.put(
    "/cycles/{cycle_id}/statutory-remittances/{category}",
    response_model=StatutoryRemittanceRow,
)
async def update_statutory_remittance_api(
    cycle_id: str,
    category: StatutoryCategory,
    req: StatutoryRemittanceUpdateRequest,
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"cfo", "finance", "admin"})
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await statutory_remittance.update_statutory_remittance(
            conn, user, cycle_id, category, req
        )


# ---------------------------------------------------------------------------
# 7. Payroll Document Vault (Secure Evidence Management)
# ---------------------------------------------------------------------------
@router.post("/documents/upload", response_model=PayrollDocumentMetadata)
async def upload_payroll_document_api(
    file: UploadFile = File(...),
    document_category: str = Form(...),
    document_type: str = Form(...),
    cycle_id: Optional[str] = Form(None),
    employee_id: Optional[str] = Form(None),
    financial_year: Optional[str] = Form(None),
    wage_month: Optional[int] = Form(None),
    wage_year: Optional[int] = Form(None),
    external_reference_number: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"cfo", "finance", "admin", "hr"}, allow_manage_payroll_perm=True)
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await document_vault.store_payroll_document(
            conn,
            user,
            file,
            document_category=document_category.upper(),  # type: ignore
            document_type=document_type.upper(),
            cycle_id=cycle_id,
            employee_id=employee_id,
            financial_year=financial_year,
            wage_month=wage_month,
            wage_year=wage_year,
            external_reference_number=external_reference_number,
            notes=notes,
        )


@router.get("/documents", response_model=list[PayrollDocumentMetadata])
async def list_payroll_documents_api(
    cycle_id: Optional[str] = Query(default=None),
    document_category: Optional[str] = Query(default=None),
    include_archived: bool = Query(default=False),
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"cfo", "finance", "admin", "hr"}, allow_manage_payroll_perm=True)
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await document_vault.list_payroll_documents(
            conn,
            cycle_id=cycle_id,
            document_category=document_category,
            include_archived=include_archived,
        )


@router.get("/documents/{document_id}/download")
async def download_payroll_document_api(
    document_id: str,
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"cfo", "finance", "admin", "hr"}, allow_manage_payroll_perm=True)
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        disk_path, filename, mime = await document_vault.get_document_for_download(
            conn, user, document_id
        )
    return FileResponse(
        path=disk_path,
        media_type=mime,
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.patch("/documents/{document_id}/archive")
async def archive_payroll_document_api(
    document_id: str,
    notes: Optional[str] = Body(default=None),
    user=Depends(get_current_user),
):
    require_payroll_roles(user, {"cfo", "admin"})
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        ok = await document_vault.archive_payroll_document(
            conn, user, document_id, notes=notes
        )
        return {"status": "success", "archived": ok}

