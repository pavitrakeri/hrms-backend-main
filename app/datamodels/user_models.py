from pydantic import BaseModel, EmailStr, Field

class ResetPasswordRequest(BaseModel):
    email: EmailStr
    new_password: str = Field(..., min_length=6)
    confirm_password: str = Field(..., min_length=6)

class ResetPasswordResponse(BaseModel):
    status: str
    message: str
