from fastapi import APIRouter, Depends
from app.db import get_db_pool
from app.features.auth import get_current_user
from app.features.payroll.initiation_service import initiate_payroll, update_payroll_details
from app.datamodels.payroll_initiation_models import PayrollInitiationRequest, PayrollInitiationResponse, PayrollUpdateRequest


router = APIRouter(prefix="/payroll/initiate", tags=["Payroll - Finance"])


@router.post("/new_employee", response_model=PayrollInitiationResponse)
async def initiate_payroll_endpoint(req: PayrollInitiationRequest, user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        result = await initiate_payroll(conn, user, req)
        return result


@router.put("/{employee_email}/update", response_model=PayrollInitiationResponse)
async def update_payroll_endpoint(employee_email: str, req: PayrollUpdateRequest, user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        result = await update_payroll_details(conn, user, employee_email, req)
        return result