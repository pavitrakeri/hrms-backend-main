from pydantic import BaseModel, EmailStr
from datetime import date, datetime
from typing import Optional, List

class AddEmployeeRequest(BaseModel):
    full_name: str
    email: EmailStr
    password: str
    manager_email: EmailStr
    hr_email: EmailStr
    department: str
    role: str   # "employee" | "line_manager" | "hr" | "admin"
    employment_status: Optional[str] = "probation"
    status: Optional[str] = None
    office_location: Optional[str] = None
    designation: Optional[str] = None
    joining_date: date
    gender: Optional[str] = None
    date_of_birth: Optional[date] = None
    marital_status: Optional[str] = None
    nationality: Optional[str] = None
    passport_number: Optional[str] = None
    emirates_id_number: Optional[str] = None
    uid_number: Optional[str] = None
    file_number: Optional[str] = None
    contract_type: Optional[str] = None
    labour_card_number: Optional[str] = None
    labour_card_expiry: Optional[date] = None
    visa_sponsorship: Optional[str] = None
    residence_visa_expiry: Optional[date] = None
    work_email: Optional[EmailStr] = None
    contact_number: Optional[str] = None
    personal_email: Optional[EmailStr] = None
    basic_salary: Optional[float] = 0
    hra: Optional[float] = 0
    mobile: Optional[float] = 0
    transportation: Optional[float] = 0
    other: Optional[float] = 0
    total_salary: Optional[float] = 0
    flight_ticket: Optional[str] = None
    wps_unique_id: Optional[str] = None
    wps: Optional[str] = None
    medical_insurance_category: Optional[str] = None
    aadhaar_card_number: Optional[str] = None
    pan_card_number: Optional[str] = None
    pf_account_number: Optional[str] = None
    esi_number: Optional[str] = None
    bank_account_number: Optional[str] = None
    ifsc_code: Optional[str] = None
    emergency_contact_name: Optional[str] = None
    emergency_contact_number: Optional[str] = None

class AddEmployeeResponse(BaseModel):
    status: str
    message: str
    employee_id: str


class EmployeeRow(BaseModel):
    id: str
    email: str
    full_name: str
    role: str
    department: Optional[str]
    manager_email: Optional[str]
    is_active: bool
    created_at: str

class EmployeeListResponse(BaseModel):
    employees: List[EmployeeRow]


class UpdateEmployeeRequest(BaseModel):
    full_name: str
    email: EmailStr
    manager_email: EmailStr
    hr_email: EmailStr
    department: str
    role: str   # "employee" | "line_manager" | "hr" | "admin" | "cfo"
    password: Optional[str] = None
    employment_status: Optional[str] = "probation"
    status: Optional[str] = None
    office_location: Optional[str] = None
    designation: Optional[str] = None
    joining_date: date
    gender: Optional[str] = None
    date_of_birth: Optional[date] = None
    marital_status: Optional[str] = None
    nationality: Optional[str] = None
    passport_number: Optional[str] = None
    emirates_id_number: Optional[str] = None
    uid_number: Optional[str] = None
    file_number: Optional[str] = None
    contract_type: Optional[str] = None
    labour_card_number: Optional[str] = None
    labour_card_expiry: Optional[date] = None
    visa_sponsorship: Optional[str] = None
    residence_visa_expiry: Optional[date] = None
    work_email: Optional[EmailStr] = None
    contact_number: Optional[str] = None
    personal_email: Optional[EmailStr] = None
    basic_salary: Optional[float] = 0
    hra: Optional[float] = 0
    mobile: Optional[float] = 0
    transportation: Optional[float] = 0
    other: Optional[float] = 0
    total_salary: Optional[float] = 0
    flight_ticket: Optional[str] = None
    wps_unique_id: Optional[str] = None
    wps: Optional[str] = None
    medical_insurance_category: Optional[str] = None
    aadhaar_card_number: Optional[str] = None
    pan_card_number: Optional[str] = None
    pf_account_number: Optional[str] = None
    esi_number: Optional[str] = None
    bank_account_number: Optional[str] = None
    ifsc_code: Optional[str] = None
    emergency_contact_name: Optional[str] = None
    emergency_contact_number: Optional[str] = None
    is_active: Optional[bool] = True
