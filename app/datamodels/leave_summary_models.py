from pydantic import BaseModel
from typing import Optional

class LeaveSummaryResponse(BaseModel):
    user_id: str
    email: Optional[str]
    total_sick_leave: float
    sick_leave_used: float
    remaining_sick_leave: float
    total_annual_leave: float
    annual_leave_used: float
    remaining_annual_leave: float
