from pydantic import BaseModel
from typing import Optional, List

class ClockInRequest(BaseModel):
    lat: float
    lon: float
    device: str | None = None


class ClockOutRequest(BaseModel):
    lat: float
    lon: float
    device: str | None = None


class AttendanceRow(BaseModel):
    id: str
    user_id: str
    employee_email: str
    clock_in_at: Optional[str]
    clock_out_at: Optional[str]
    total_seconds: Optional[int]
    created_at: str

class AttendanceListResponse(BaseModel):
    records: List[AttendanceRow]