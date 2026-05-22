from pydantic import BaseModel
from typing import Optional

class RoleCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None

class RoleCreateResponse(BaseModel):
    status: str
    message: str
    role_id: str
