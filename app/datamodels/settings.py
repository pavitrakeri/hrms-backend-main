from pydantic import BaseModel, EmailStr, Field
from typing import Optional

class UpdateProfileRequest(BaseModel):
    personal_email: Optional[EmailStr] = None
    contact_number: Optional[str] = None
    marital_status: Optional[str] = None
    emergency_contact_name: Optional[str] = None
    emergency_contact_number: Optional[str] = None
    bank_account_number: Optional[str] = None

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=6)
    confirm_password: str = Field(..., min_length=6)

class CompanySettingsRequest(BaseModel):
    company_name: str
    office_start_time: str
    office_end_time: str
    weekend_days: str
    currency: str

class CompanySettingsResponse(BaseModel):
    company_name: str
    office_start_time: str
    office_end_time: str
    weekend_days: str
    currency: str
