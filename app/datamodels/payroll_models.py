from pydantic import BaseModel
from typing import List, Optional
from datetime import date, datetime


class PayslipSummary(BaseModel):
    payroll_item_id: str
    month: date
    basic: float
    gross_pay: float
    total_deductions: float
    net_pay: float
    payslip_url: Optional[str]
    status: str


class PayslipListResponse(BaseModel):
    status: str
    payslips: List[PayslipSummary]


class PayslipDetailResponse(BaseModel):
    status: str
    payroll_item_id: str
    month: date
    basic: float
    gross_pay: float
    total_deductions: float
    net_pay: float
    allowances: Optional[dict]
    deductions: Optional[dict]
    payslip_url: Optional[str]




class PayrollApplyRequest(BaseModel):
    request_type: str  # 'advance' | 'certificate' | 'query' | 'schedule_change'
    amount: Optional[float] = None
    purpose: Optional[str] = None
    query_type: Optional[str] = None
    reason: Optional[str] = None
    description: Optional[str] = None
    requested_date: Optional[date] = None
    attachments: Optional[List[str]] = None  # URLs after upload

class PayrollResponse(BaseModel):
    status: str
    message: str
    request_id: Optional[str]

class PayrollListItem(BaseModel):
    id: str
    request_type: str
    status: str
    current_approver_role: Optional[str]
    created_at: datetime
    amount: Optional[float] = None

class PayrollListResponse(BaseModel):
    requests: List[PayrollListItem]
