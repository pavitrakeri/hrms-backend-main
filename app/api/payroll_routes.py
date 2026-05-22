from fastapi import APIRouter, Depends, Query, UploadFile, File, Form, BackgroundTasks, HTTPException
from typing import List, Optional
from datetime import date
from app.db import get_db_pool
from app.features.payroll.payslip_service import get_payslip_list, get_payslip_detail, get_current_month_payslip
from app.datamodels.payroll_models import PayslipListResponse, PayslipDetailResponse
from app.features.auth import get_current_user  # assuming existing auth dependency
from app.features.payroll.payroll_request import apply_payroll_request
from app.datamodels.payroll_models import PayrollApplyRequest, PayrollResponse
from app.features.payroll.payroll_calculator import run_monthly_payroll


router = APIRouter(prefix="/payroll", tags=["Payroll"])


@router.get("/me/payslips", response_model=PayslipListResponse)
async def list_my_payslips(
    month: date = Query(None, description="Optional month filter (YYYY-MM-DD)"),
    user=Depends(get_current_user)
):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        payslips = await get_payslip_list(conn, user, month)
        return {"status": "success", "payslips": payslips}


@router.get("/me/payslip/{payroll_item_id}", response_model=PayslipDetailResponse)
async def view_payslip_detail(payroll_item_id: str, user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        detail = await get_payslip_detail(conn, user, payroll_item_id)
        return detail


@router.get("/me/payslip-current", response_model=PayslipDetailResponse)
async def current_month_payslip(user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        result = await get_current_month_payslip(conn, user)
        return result




@router.post("/apply", response_model=PayrollResponse)
async def apply_payroll(
    background_tasks: BackgroundTasks,
    request_type: str = Form(...),
    amount: Optional[float] = Form(None),
    purpose: Optional[str] = Form(None),
    query_type: Optional[str] = Form(None),
    reason: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    requested_date: Optional[date] = Form(None),
    attachments: Optional[List[UploadFile]] = File(None),
    user=Depends(get_current_user)
):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        req_obj = PayrollApplyRequest(
            request_type=request_type,
            amount=amount,
            purpose=purpose,
            query_type=query_type,
            reason=reason,
            description=description,
            requested_date=requested_date,
            attachments=attachments
        )
        res = await apply_payroll_request(conn, user, req_obj, background_tasks)
        return res


@router.get("/my")
async def list_my_payroll_requests(user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, request_type, status, current_approver_role, created_at, amount
            FROM payroll_requests WHERE employee_id=$1 ORDER BY created_at DESC
        """, user["id"])
        return [{"id": str(r["id"]),
                 "request_type": r["request_type"],
                 "status": r["status"],
                 "current_approver_role": r["current_approver_role"],
                 "amount": r["amount"],
                 "created_at": r["created_at"].isoformat()} for r in rows]


@router.post("/admin/run-payroll", tags=["Admin"])
async def trigger_monthly_payroll(
    month: date = Query(..., description="Payroll month (YYYY-MM-DD, use 1st of month)"),
    user=Depends(get_current_user)
):
    """
    Admin/Finance endpoint to manually trigger monthly payroll calculation.
    Calculates payroll for all employees for the specified month.
    """
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        # Check permissions
        role = await conn.fetchval("""
            SELECT r.name FROM roles r
            JOIN users u ON u.role_id = r.id
            WHERE u.id=$1
        """, user["id"])
        
        if role not in ("admin", "finance", "cfo"):
            raise HTTPException(status_code=403, detail="Only Admin/Finance/CFO can run payroll")
        
        try:
            await run_monthly_payroll(conn, month)
            return {
                "status": "success",
                "message": f"Payroll calculation completed for {month.strftime('%Y-%m')}",
                "month": month.strftime('%Y-%m')
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Payroll calculation failed: {str(e)}")
