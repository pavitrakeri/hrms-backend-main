# app/api/reimbursement_routes.py
from fastapi import APIRouter, Depends, UploadFile, File, Form, BackgroundTasks, HTTPException
from typing import List, Optional
from app.db import get_db_pool
from app.features.auth import get_current_user
from app.features.reimbursement.service import (
    apply_reimbursement, list_my_reimbursements, edit_reimbursement,
    get_reimbursement_detail, approve_reimbursement, reject_reimbursement
)
from app.datamodels.reimbursement_models import (
    ReimbursementApplyResponse, ReimbursementListResponse, ReimbursementDetailResponse
)
from datetime import datetime


def _parse_expense_date(expense_date: str):
    formats = ["%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"]
    for fmt in formats:
        try:
            return datetime.strptime(expense_date.strip(), fmt).date()
        except ValueError:
            continue
    raise HTTPException(status_code=400, detail="Invalid expense_date format. Use YYYY-MM-DD or DD-MM-YYYY")


router = APIRouter(prefix="/reimbursements", tags=["Reimbursements"])

@router.post("/apply", response_model=ReimbursementApplyResponse)
async def apply_endpoint(
    background_tasks: BackgroundTasks,
    category: str = Form(...),
    subcategory: Optional[str] = Form(None),
    amount: float = Form(...),
    description: Optional[str] = Form(None),
    expense_date: str = Form(...),  # Accept multiple formats
    files: List[UploadFile] = File(...),
    user=Depends(get_current_user)
):
    expense_dt = _parse_expense_date(expense_date)
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        res = await apply_reimbursement(conn, user, category, subcategory, amount, description, expense_dt, files, background_tasks)
        return res

@router.get("/my", response_model=ReimbursementListResponse)
async def my_reimbursements(user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        rows = await list_my_reimbursements(conn, user)
        return {"status": "success", "reimbursements": rows}

@router.get("/{reimbursement_id}", response_model=ReimbursementDetailResponse)
async def reimbursement_detail(reimbursement_id: str, user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        detail = await get_reimbursement_detail(conn, user, reimbursement_id)
        return detail

@router.put("/{reimbursement_id}")
async def edit_endpoint(
    reimbursement_id: str,
    category: str = Form(...),
    subcategory: Optional[str] = Form(None),
    amount: float = Form(...),
    description: Optional[str] = Form(None),
    expense_date: str = Form(...),
    files: Optional[List[UploadFile]] = None,
    user=Depends(get_current_user)
):
    expense_dt = _parse_expense_date(expense_date)
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await edit_reimbursement(conn, user, reimbursement_id, category, subcategory, amount, description, expense_dt, files)

# Approve endpoint (for manager/finance/cfo)
@router.post("/{reimbursement_id}/approve")
async def approve_endpoint(reimbursement_id: str, background_tasks: BackgroundTasks, comment: Optional[str] = Form(None), user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await approve_reimbursement(conn, user, reimbursement_id, comment, background_tasks)

@router.post("/{reimbursement_id}/reject")
async def reject_endpoint(reimbursement_id: str, background_tasks: BackgroundTasks, comment: Optional[str] = Form(None), user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await reject_reimbursement(conn, user, reimbursement_id, comment, background_tasks)


@router.post("/{reimbursement_id}/query")
async def query_endpoint(
    reimbursement_id: str,
    background_tasks: BackgroundTasks,  
    comment: Optional[str] = Form(None),
    user=Depends(get_current_user)
):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        from app.features.reimbursement.service import query_reimbursement
        return await query_reimbursement(conn, user, reimbursement_id, comment, background_tasks)
