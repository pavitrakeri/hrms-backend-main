from pydantic import BaseModel
from typing import Optional

class RoleCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None

class RoleCreateResponse(BaseModel):
    status: str
    message: str
    role_id: str

class RoleUpdateRequest(BaseModel):
    role_id: str
    name: Optional[str] = None
    description: Optional[str] = None

class RoleUpdateResponse(BaseModel):
    status: str
    message: str
    role_id: str
