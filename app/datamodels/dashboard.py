from pydantic import BaseModel
from typing import Optional, List

class UserSummary(BaseModel):
    full_name: str
    email: str
    role: str
    department: Optional[str]

class AttendanceSummary(BaseModel):
    clock_in_at: Optional[str]
    clock_out_at: Optional[str]
    status: str

class MyStatsSummary(BaseModel):
    tasks_count: int
    projects_count: int
    annual_leave_remaining: float
    sick_leave_remaining: float

class OrgStatsSummary(BaseModel):
    total_employees: int
    clocked_in_today: int
    pending_leaves: int
    pending_reimbursements: int
    pending_resignations: int
    pending_recruitments: int

class HolidayRow(BaseModel):
    name: str
    date: str

class PendingActionRow(BaseModel):
    type: str  # "leave" | "reimbursement" | "resignation" | "recruitment"
    id: str
    employee_name: str
    details: str
    amount: Optional[float] = None
    date_info: Optional[str] = None

class DashboardSummaryResponse(BaseModel):
    user: UserSummary
    attendance_today: AttendanceSummary
    my_stats: MyStatsSummary
    upcoming_holidays: List[HolidayRow]
    org_stats: Optional[OrgStatsSummary] = None
    pending_actions: Optional[List[PendingActionRow]] = None
