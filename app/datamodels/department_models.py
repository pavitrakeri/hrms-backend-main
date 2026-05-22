from pydantic import BaseModel, EmailStr
from typing import Optional


class DepartmentCreateRequest(BaseModel):
    name: str
    manager_email: Optional[EmailStr] = None
    hr_email: Optional[EmailStr] = None


class DepartmentCreateResponse(BaseModel):
    status: str
    message: str
    department_id: str


class DepartmentUpdateRequest(BaseModel):
    department_id: str
    name: Optional[str] = None
    manager_email: Optional[EmailStr] = None
    hr_email: Optional[EmailStr] = None

class DepartmentUpdateResponse(BaseModel):
    status: str
    message: str
    department_id: str