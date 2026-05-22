from pydantic import BaseModel
from typing import Optional, List

class PolicyRow(BaseModel):
    policy_id: str
    title: str
    description: Optional[str]
    file_url: Optional[str]
    acknowledged: bool
    acknowledged_at: Optional[str]

class PolicyListResponse(BaseModel):
    policies: List[PolicyRow]

class PolicyAcknowledgeResponse(BaseModel):
    status: str
    message: str
    policy_id: str


class PolicyUploadResponse(BaseModel):
    status: str
    message: str
    policy_id: str
    assigned_count: int