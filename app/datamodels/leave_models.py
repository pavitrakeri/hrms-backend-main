# app/datamodels/leave_models.py
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import date

class LeaveApplyRequest(BaseModel):
    leave_type_id: int
    start_date: date
    end_date: date
    reason: str = Field(..., min_length=3)
    half_day: bool = False
    half_day_slot: Optional[str] = None  # "first"|"second"

class BasicResponse(BaseModel):
    status: str
    message: str

class LeaveApplyResponse(BasicResponse):
    leave_id: Optional[str] = None

class ApproveRejectRequest(BaseModel):
    # nothing extra required: approver is inferred from JWT + leave_id path param
    comment: Optional[str] = None

class LeaveApprovalRow(BaseModel):
    approver_id: str
    approver_role: str
    decision: str
    decided_at: Optional[str]

class LeaveStatusResponse(BaseModel):
    leave_id: str
    status: str
    approvals: List[LeaveApprovalRow]

class MyApprovalRow(BaseModel):
    leave_id: str
    applicant_email: str
    leave_type: str
    start_date: date
    end_date: date
    status: str           # overall leave status
    my_decision: str      # approver’s decision (pending/approved/rejected)


class MyLeaveApproval(BaseModel):
    approver_email: str
    approver_role: str
    decision: str
    decided_at: Optional[str]

class MyLeaveRow(BaseModel):
    leave_id: str
    leave_type: str
    start_date: date
    end_date: date
    reason: Optional[str] = None
    status: str
    approvals: List[MyLeaveApproval]

class HolidayRow(BaseModel):
    date: str
    name: str

class CalendarLeaveRow(BaseModel):
    employee: str
    leave_type: str
    start_date: str
    end_date: str

class LeaveCalendarResponse(BaseModel):
    month: str
    holidays: List[HolidayRow]
    approved_leaves: List[CalendarLeaveRow]


class CancelLeaveResponse(BaseModel):
    status: str
    message: str
    leave_id: str

class LeaveApplyRequest(BaseModel):
    leave_type_id: int
    start_date: date
    end_date: date
    half_day: bool = False
    half_day_slot: Optional[str] = None
    reason: Optional[str] = None
    medical_document_url: Optional[str] = None   # for sick leaves > 1 day


class LeaveApplyResponse(BaseModel):
    status: str
    message: str
    leave_id: str
    medical_document_url: Optional[str] = None