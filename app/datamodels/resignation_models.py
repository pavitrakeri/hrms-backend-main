from pydantic import BaseModel
from typing import List, Optional
from datetime import date

class ResignationApplyRequest(BaseModel):
    reason: str
    last_working_day: date

class ApproveRejectRequest(BaseModel):
    comment: Optional[str] = None

class CancelResignationResponse(BaseModel):
    status: str
    message: str
    resignation_id: str

class ResignationApprovalRow(BaseModel):
    approver_email: str
    approver_role: str
    decision: str
    decided_at: Optional[str]

class MyResignationRow(BaseModel):
    resignation_id: str
    reason: str
    last_working_day: date
    status: str
    approvals: List[ResignationApprovalRow]


class MyResignationApprovalRow(BaseModel):
    resignation_id: str
    applicant_email: str
    reason: str
    last_working_day: date
    status: str          # overall resignation status
    my_decision: str     # this approver's decision
