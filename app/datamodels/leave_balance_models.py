from pydantic import BaseModel
from typing import Optional, List

class LeaveBalanceItem(BaseModel):
    leave_type: str
    total_entitled: float
    used_days: float
    carried_forward: float
    remaining: float

class LeaveBalanceResponse(BaseModel):
    user_id: str
    email: Optional[str]
    balances: List[LeaveBalanceItem]
