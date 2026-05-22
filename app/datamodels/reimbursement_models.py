# app/datamodels/reimbursement_models.py
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import date

class ReimbursementFile(BaseModel):
    name: str
    url: str
    path: Optional[str] = None

class ReimbursementApplyResponse(BaseModel):
    status: str
    message: str
    reimbursement_id: str

class ReimbursementListItem(BaseModel):
    reimbursement_id: str
    category: str
    subcategory: Optional[str] = None
    amount: float
    expense_date: date
    status: str
    pending_with: Optional[str] = None
    supporting_docs: Optional[List[ReimbursementFile]] = None

class ReimbursementListResponse(BaseModel):
    status: str
    reimbursements: List[ReimbursementListItem]

class ReimbursementDetailResponse(BaseModel):
    reimbursement_id: str
    user_id: str
    category: str
    subcategory: Optional[str] = None
    amount: float
    description: Optional[str]
    expense_date: date
    supporting_docs: List[ReimbursementFile]
    status: str
    approvals: List[dict]
    created_at: str
    decided_at: Optional[str] = None
