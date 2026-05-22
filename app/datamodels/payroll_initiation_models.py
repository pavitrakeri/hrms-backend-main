from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime

class PayrollInitiationRequest(BaseModel):
    employee_email: str
    basic_salary: float
    hra: Optional[float] = 0.0
    allowances: Optional[float] = 0.0
    other_benefits: Optional[float] = 0.0
    payment_mode: Optional[str] = "bank_transfer"
    bank_account_number: Optional[str] = None
    bank_name: Optional[str] = None
    iban_number: Optional[str] = None
    remarks: Optional[str] = None

class PayrollInitiationResponse(BaseModel):
    status: str
    message: str
    payroll_id: Optional[str] = None


class PayrollUpdateRequest(BaseModel):
    basic_salary: Optional[float] = None
    hra: Optional[float] = None
    allowances: Optional[float] = None
    other_benefits: Optional[float] = None
    payment_mode: Optional[str] = None
    bank_account_number: Optional[str] = None
    bank_name: Optional[str] = None
    iban_number: Optional[str] = None
    remarks: Optional[str] = None
