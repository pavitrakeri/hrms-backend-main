from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import date, datetime

class ClockInRequest(BaseModel):
    lat: float
    lon: float
    device: Optional[str] = None


class ClockOutRequest(BaseModel):
    lat: float
    lon: float
    device: Optional[str] = None


class AttendanceRow(BaseModel):
    id: str
    user_id: str
    employee_email: str
    employee_name: Optional[str] = None
    clock_in_at: Optional[str] = None
    clock_out_at: Optional[str] = None
    total_seconds: Optional[int] = None
    location: Optional[str] = None
    is_regularised: Optional[bool] = False
    created_at: Optional[str] = None

class AttendanceListResponse(BaseModel):
    records: List[AttendanceRow]


class RegularisationApplyRequest(BaseModel):
    request_date: date
    requested_clock_in: datetime
    requested_clock_out: datetime
    reason: str = Field(..., min_length=3)
    attendance_id: Optional[str] = None


class RegularisationActionRequest(BaseModel):
    action: str  # "approve" | "reject"
    rejection_reason: Optional[str] = None


class RegularisationRow(BaseModel):
    id: str
    user_id: str
    employee_name: Optional[str] = None
    employee_email: Optional[str] = None
    department_name: Optional[str] = None
    request_date: str
    requested_clock_in: str
    requested_clock_out: str
    reason: str
    status: str
    attendance_id: Optional[str] = None
    rejection_reason: Optional[str] = None
    reviewed_by: Optional[str] = None
    reviewer_name: Optional[str] = None
    reviewed_at: Optional[str] = None
    created_at: str


class AttendanceReportSummary(BaseModel):
    total_records: int
    completed_sessions: int
    in_progress_sessions: int
    total_seconds: int
    average_seconds: int
    unique_employees_present: int
    pending_regularisations: int


class AttendanceReportRow(BaseModel):
    id: str
    user_id: str
    employee_name: str
    employee_email: str
    department_id: Optional[str] = None
    department_name: Optional[str] = None
    date: str
    clock_in_at: Optional[str] = None
    clock_out_at: Optional[str] = None
    total_seconds: Optional[int] = None
    location: Optional[str] = None
    is_regularised: bool = False
    status: str  # "completed" | "in_progress"
    created_at: Optional[str] = None


class AttendanceReportResponse(BaseModel):
    summary: AttendanceReportSummary
    records: List[AttendanceReportRow]
    total_count: int