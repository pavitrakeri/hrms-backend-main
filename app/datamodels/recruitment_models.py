from pydantic import BaseModel
from typing import Optional, List
from datetime import date

class RecruitmentRequest(BaseModel):
    position: str
    department: str
    budget: int
    job_description: str

class RecruitmentResponse(BaseModel):
    status: str
    message: str
    recruitment_id: str

class RecruitmentApprovalRow(BaseModel):
    approver_email: str
    approver_role: str
    decision: str
    decided_at: Optional[str]

class MyRecruitmentRow(BaseModel):
    recruitment_id: str
    position: str
    department: str
    budget: float
    job_description: str
    status: str
    approvals: List[RecruitmentApprovalRow]
