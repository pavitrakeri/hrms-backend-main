from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks, Form, UploadFile, File, Request
from fastapi.responses import JSONResponse
import uvicorn
import logging
# Added new endpoints: GET /departments/list, GET /roles/list
from .db import init_db, close_db, get_db_pool
from .features.auth import LoginRequest, TokenResponse, verify_password, create_access_token, get_current_user
from app.datamodels.user_models import ResetPasswordRequest, ResetPasswordResponse
from app.features.auth import reset_password

from .datamodels.attendance import ClockInRequest, ClockOutRequest, AttendanceListResponse
from .features.attendance import get_latest_attendance
from app.datamodels.leave_models import (LeaveApplyRequest, ApproveRejectRequest, LeaveApplyResponse,
                                          LeaveStatusResponse, MyApprovalRow, MyLeaveRow, LeaveCalendarResponse, CancelLeaveResponse)
from app.features.leaves import (apply_leave_with_upload, approve_leave, reject_leave, get_my_approvals, get_my_leaves, 
                                 get_leave_calendar, cancel_leave)
from app.features.uploads import upload_medical_document

from app.datamodels.resignation_models import (ResignationApplyRequest, ApproveRejectRequest, CancelResignationResponse,
                                                MyResignationRow, MyResignationApprovalRow)
from app.features.resignations import (apply_resignation, approve_resignation, reject_resignation, cancel_resignation,
                                        get_my_resignations, get_my_resignation_approvals)

from app.datamodels.employee_models import AddEmployeeRequest, AddEmployeeResponse, EmployeeListResponse, UpdateEmployeeRequest
from app.features.employees import add_employee, list_employees, get_employee_details, update_employee

from app.datamodels.policy_models import PolicyListResponse, PolicyAcknowledgeResponse, PolicyUploadResponse
from app.features.policies import get_my_policies, acknowledge_policy, upload_policy

from app.datamodels.recruitment_models import RecruitmentRequest, RecruitmentResponse, MyRecruitmentRow
from app.features.recruitments import raise_recruitment, approve_recruitment, reject_recruitment, get_my_recruitments, get_my_recruitment_approvals

from app.features.leave_accrual import accrue_annual_leave, carry_forward_annual_leave
from app.features.auth import get_current_user

from app.features.leaves_balance import get_leave_balance
from app.datamodels.leave_balance_models import LeaveBalanceResponse

from app.features.leaves_summary import get_leave_summary
from app.datamodels.leave_summary_models import LeaveSummaryResponse

from app.datamodels.department_models import DepartmentCreateRequest, DepartmentCreateResponse, DepartmentUpdateRequest, DepartmentUpdateResponse
from app.features.departments import create_department, update_department, delete_department
from app.features.departments_list import list_departments

from app.datamodels.role_models import RoleCreateRequest, RoleCreateResponse, RoleUpdateRequest, RoleUpdateResponse
from app.features.roles import create_role, update_role, delete_role
from app.features.roles_list import list_roles

from pydantic import BaseModel
from datetime import datetime, timezone, date
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional

from app.utils.geocode import reverse_geocode

from app.api import payroll_routes
from app.api import reimbursement_routes
from app.api import payroll_initiation_routes
from app.api import projects_routes


app = FastAPI(title="HRMS API")

# CORS configuration - allow any origin to connect to the backend
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex="https?://.*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    if isinstance(exc, HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    logging.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "message": str(exc)},
    )


@app.on_event("startup")
async def startup():
    await init_db()

@app.on_event("shutdown")
async def shutdown():
    await close_db()


app.include_router(payroll_routes.router)
app.include_router(reimbursement_routes.router)
app.include_router(payroll_initiation_routes.router)
app.include_router(projects_routes.router)

@app.post("/login", response_model=TokenResponse, tags=["Auth"])
async def login(req: LoginRequest):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT u.id, u.email, u.password_hash, r.name as role, u.password_reset_required
            FROM users u
            LEFT JOIN roles r ON u.role_id = r.id
            WHERE u.email=$1 AND u.is_active=true
        """, req.email)

    if not row:
        raise HTTPException(status_code=401, detail="User not found")
    if not verify_password(req.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    reset_req = bool(row["password_reset_required"]) if row["password_reset_required"] is not None else False
    token, expiry = create_access_token(str(row["id"]), row["role"], row["email"], reset_req)
    return {
        "access_token": token,
        "expires_at": expiry,
        "role": row["role"],
        "password_reset_required": reset_req
    }


@app.post("/attendance/clockin", tags=["Attendance"])
async def clockin(req: ClockInRequest, user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        # check if already clocked in today
        existing = await conn.fetchrow("""
            SELECT id FROM attendance
            WHERE user_id=$1 AND date_trunc('day', clock_in_at) = date_trunc('day', now())
        """, user["id"])
        if existing:
            return {"message": "Already clocked in today"}

        # reverse geocode lat/lon to address
        location = await reverse_geocode(req.lat, req.lon)

        # insert attendance with location data
        await conn.execute("""
            INSERT INTO attendance (id, user_id, clock_in_at, lat, lon, location)
            VALUES (gen_random_uuid(), $1, $2, $3, $4, $5)
        """, user["id"], datetime.utcnow(), req.lat, req.lon, location)

    return {
        "message": "Clock-in successful",
        "user": user["email"]
    }

@app.get("/attendance/my-location", tags=["Attendance"])
async def get_my_location(user=Depends(get_current_user)):
    db_pool = get_db_pool()
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT lat, lon, location, clock_in_at
                FROM attendance
                WHERE user_id=$1
                  AND date_trunc('day', clock_in_at) = date_trunc('day', now())
                ORDER BY clock_in_at DESC
                LIMIT 1
            """, user["id"])

            if not row:
                raise HTTPException(status_code=404, detail="You have not clocked in today")

            return {
                "lat": row["lat"],
                "lon": row["lon"],
                "location": row["location"],
                "clock_in_at": row["clock_in_at"].isoformat() if row["clock_in_at"] else None
            }

    except HTTPException as he:
        # Pass through cleanly
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch location: {str(e)}")
    
    
@app.post("/attendance/clockout", tags=["Attendance"])
async def clockout(req: ClockOutRequest, user=Depends(get_current_user)):
    try:
        db_pool = get_db_pool()
        async with db_pool.acquire() as conn:
            # find today's clock-in
            record = await conn.fetchrow("""
                SELECT id, clock_in_at, clock_out_at
                FROM attendance
                WHERE user_id=$1
                AND (clock_in_at AT TIME ZONE 'UTC' + interval '5 hours 30 minutes')::date = CURRENT_DATE
                ORDER BY clock_in_at DESC
                LIMIT 1
            """, user["id"])

            if not record:
                return {"status": "fail", "message": "No clock-in found for today"}

            if record["clock_out_at"] is not None:
                return {"status": "fail", "message": "Already clocked out today"}

            # calculate total time
            clock_in_time = record["clock_in_at"]
            clock_out_time = datetime.now(timezone.utc)
            total_seconds = int((clock_out_time - clock_in_time).total_seconds())

            # update record
            await conn.execute("""
                UPDATE attendance
                SET clock_out_at=$1, total_seconds=$2
                WHERE id=$3
            """, clock_out_time, total_seconds, record["id"])

        return {
            "status": "success",
            "message": "Clock-out successful",
            "user": user["email"],
            "clock_in": clock_in_time.isoformat(),
            "clock_out": clock_out_time.isoformat(),
            "total_seconds": total_seconds
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Clock-out failed: {str(e)}")


@app.get("/attendance/latest", response_model=AttendanceListResponse, tags=["Attendance"])
async def attendance_latest(limit: int = 10, employee_id: str = None, user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        records = await get_latest_attendance(conn, user, limit, employee_id)
        return {"records": records}

@app.post("/leaves/apply", response_model=LeaveApplyResponse, tags=["Leave"])
async def leaves_apply(
    bg: BackgroundTasks,
    user=Depends(get_current_user),
    leave_type_id: int = Form(...),
    start_date: date = Form(...),
    end_date: date = Form(...),
    half_day: bool = Form(False),
    half_day_slot: str | None = Form(None),
    reason: str | None = Form(None),
    file: UploadFile | None = File(None)
):
    """
    Apply for leave:
    - Handles all validations
    - Uploads medical document to Supabase for sick > 1 day
    - Inserts leave + approvals + notifications
    """
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        try:
            res = await apply_leave_with_upload(
                conn,
                user,
                bg,
                leave_type_id=leave_type_id,
                start_date=start_date,
                end_date=end_date,
                half_day=half_day,
                half_day_slot=half_day_slot,
                reason=reason,
                file=file
            )

            return {
                "status": res["status"],
                "message": res["message"],
                "leave_id": res["leave_id"],
                "medical_document_url": res.get("medical_document_url")
            }

        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Leave apply failed: {str(e)}")
        

@app.post("/leaves/{leave_id}/approve", tags=["Leave"])
async def leaves_approve(leave_id: str, req: ApproveRejectRequest, bg: BackgroundTasks, user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await approve_leave(conn, user, leave_id, req.comment, bg)

@app.post("/leaves/{leave_id}/reject", tags=["Leave"])
async def leaves_reject(leave_id: str, req: ApproveRejectRequest, bg: BackgroundTasks, user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await reject_leave(conn, user, leave_id, req.comment, bg)

@app.get("/leaves/{leave_id}/status", response_model=LeaveStatusResponse, tags=["Leave"])
async def leaves_status(leave_id: str, user=Depends(get_current_user)):
    try:
        db_pool = get_db_pool()
        async with db_pool.acquire() as conn:
            leave = await conn.fetchrow("SELECT id, status FROM leaves WHERE id=$1", leave_id)
            if not leave:
                raise HTTPException(status_code=404, detail="Leave not found")
            approvals = await conn.fetch("""
                SELECT approver_id, approver_role, decision, decided_at
                FROM leave_approvals WHERE leave_id=$1 ORDER BY decided_at NULLS FIRST
            """, leave_id)
            rows = []
            for a in approvals:
                rows.append({
                    "approver_id": str(a["approver_id"]),
                    "approver_role": a["approver_role"],
                    "decision": a["decision"],
                    "decided_at": a["decided_at"].isoformat() if a["decided_at"] else None
                })
            return {"leave_id": str(leave["id"]), "status": leave["status"], "approvals": rows}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Status failed: {str(e)}")
    

@app.get("/approvals/my", response_model=List[MyApprovalRow], tags=["Leave"])
async def approvals_my(user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await get_my_approvals(conn, user)
    

@app.get("/leaves/my", response_model=List[MyLeaveRow], tags=["Leave"])
async def my_leaves(user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await get_my_leaves(conn, user)
    

@app.get("/leave/calendar", response_model=LeaveCalendarResponse, tags=["Leave"])
async def leave_calendar(month: str = None, user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await get_leave_calendar(conn, month)
    

@app.post("/leaves/{leave_id}/cancel", response_model=CancelLeaveResponse, tags=["Leave"])
async def leaves_cancel(leave_id: str, bg: BackgroundTasks, user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await cancel_leave(conn, user, leave_id, bg)
    

@app.post("/resignations/apply", tags=["Resignation"])
async def resignation_apply(req: ResignationApplyRequest, bg: BackgroundTasks, user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await apply_resignation(conn, user, req, bg)

@app.post("/resignations/{resignation_id}/approve", tags=["Resignation"])
async def resignation_approve(resignation_id: str, bg: BackgroundTasks, user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await approve_resignation(conn, user, resignation_id, bg)

@app.post("/resignations/{resignation_id}/reject", tags=["Resignation"])
async def resignation_reject(resignation_id: str, req: ApproveRejectRequest, bg: BackgroundTasks, user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await reject_resignation(conn, user, resignation_id, req.comment, bg)

@app.post("/resignations/{resignation_id}/cancel", response_model=CancelResignationResponse, tags=["Resignation"])
async def resignation_cancel(resignation_id: str, bg: BackgroundTasks, user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await cancel_resignation(conn, user, resignation_id, bg)

@app.get("/resignations/my", response_model=List[MyResignationRow], tags=["Resignation"])
async def resignation_my(user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await get_my_resignations(conn, user)
    

@app.get("/approvals/resignations/my", response_model=List[MyResignationApprovalRow], tags=["Resignation"])
async def resignation_approvals_my(user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await get_my_resignation_approvals(conn, user)
    

@app.post("/employees/add", response_model=AddEmployeeResponse, tags=["Employee"])
async def employees_add(req: AddEmployeeRequest, bg: BackgroundTasks, user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await add_employee(conn, user, req, bg)
    
@app.get("/employees/list", response_model=EmployeeListResponse, tags=["Employee"])
async def employees_list(department: str = None, role: str = None, active: bool = None, user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        records = await list_employees(conn, user, department, role, active)
        return {"employees": records}

@app.get("/employees/{employee_id}", tags=["Employee"])
async def get_employee(employee_id: str, user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await get_employee_details(conn, user, employee_id)

@app.put("/employees/{employee_id}", tags=["Employee"])
async def update_employee_endpoint(employee_id: str, req: UpdateEmployeeRequest, user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await update_employee(conn, user, employee_id, req)
    
@app.get("/policies/my", response_model=PolicyListResponse, tags=["Policy"])
async def my_policies(user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        records = await get_my_policies(conn, user)
        return {"policies": records}

@app.post("/policies/{policy_id}/acknowledge", response_model=PolicyAcknowledgeResponse, tags=["Policy"])
async def policies_acknowledge(policy_id: str, user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await acknowledge_policy(conn, user, policy_id)
    

@app.post("/policies/upload", response_model=PolicyUploadResponse, tags=["Policy"])
async def policies_upload(
    title: str = Form(...),
    description: str = Form(""),
    roles: str = Form(None),          # e.g. "employee,hr"
    departments: str = Form(None),    # e.g. "Tech,Finance"
    file: UploadFile = File(...),
    user=Depends(get_current_user)
):
    roles_list = roles.split(",") if roles else []
    dept_list = departments.split(",") if departments else []
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await upload_policy(conn, user, title, description, file, roles_list, dept_list)
    
@app.post("/recruitments/request", response_model=RecruitmentResponse, tags=["Recruitment"])
async def recruitment_request(req: RecruitmentRequest, bg: BackgroundTasks, user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await raise_recruitment(conn, user, req, bg)

@app.post("/recruitments/{recruitment_id}/approve", tags=["Recruitment"])
async def recruitment_approve(recruitment_id: str, bg: BackgroundTasks, user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await approve_recruitment(conn, user, recruitment_id, bg)

@app.post("/recruitments/{recruitment_id}/reject", tags=["Recruitment"])
async def recruitment_reject(recruitment_id: str, bg: BackgroundTasks, user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await reject_recruitment(conn, user, recruitment_id, bg)

@app.get("/recruitments/my", response_model=List[MyRecruitmentRow], tags=["Recruitment"])
async def my_recruitments(user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await get_my_recruitments(conn, user)

@app.get("/approvals/recruitments/my", tags=["Recruitment"])
async def my_recruitment_approvals(user=Depends(get_current_user)):
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await get_my_recruitment_approvals(conn, user)
    

@app.post("/leaves/accrue-monthly", tags=["Admin"])
async def accrue_leaves_admin(user=Depends(get_current_user)):
    if user["role_name"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    await accrue_annual_leave()
    return {"message": "Monthly accrual completed"}


@app.post("/leaves/carry-forward", tags=["Admin"])
async def carry_forward_leaves_admin(user=Depends(get_current_user)):
    if user["role_name"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    await carry_forward_annual_leave()
    return {"message": "Year-end carry forward completed"}

@app.get("/leaves/balance", response_model=LeaveBalanceResponse, tags=["Leave"])
async def leaves_balance(employee_id: Optional[str] = None, user=Depends(get_current_user)):
    """
    Get leave balances (sick and annual).
    - Employees: their own balance
    - HR/Admin/Manager: can view others’ via ?employee_id=
    """
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await get_leave_balance(conn, user, employee_id)

@app.get("/leaves/summary", response_model=LeaveSummaryResponse, tags=["Leave"])
async def leaves_summary(employee_id: Optional[str] = None, user=Depends(get_current_user)):
    """
    Get summarized sick and annual leave stats:
    - total
    - used
    - remaining
    HR/Admin/Managers can query others via ?employee_id=
    """
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await get_leave_summary(conn, user, employee_id)
    
@app.post("/leaves/upload-medical-doc", tags=["Leave"])
async def upload_medical_doc(file: UploadFile = File(...), user=Depends(get_current_user)):
    """
    Upload medical document for sick leave.
    Returns a Supabase public URL.
    """
    return await upload_medical_document(user, file)

@app.post("/departments/create", response_model=DepartmentCreateResponse, tags=["Departments"])
async def departments_create(req: DepartmentCreateRequest, user=Depends(get_current_user)):
    """
    Create a new department.
    Only Admin or HR can access this.
    """
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await create_department(conn, user, req)

@app.get("/departments/list", tags=["Departments"])
async def departments_list(user=Depends(get_current_user)):
    """
    Get all departments.
    Accessible to Admin, HR, and managers.
    """
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return {"departments": await list_departments(conn, user)}

@app.put("/departments/update", response_model=DepartmentUpdateResponse, tags=["Departments"])
async def departments_update(req: DepartmentUpdateRequest, user=Depends(get_current_user)):
    """
    Update an existing department.
    Only Admin or HR can access this.
    """
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await update_department(conn, user, req)

@app.delete("/departments/{department_id}", tags=["Departments"])
async def departments_delete(department_id: str, user=Depends(get_current_user)):
    """
    Delete an existing department.
    Only Admin or HR can access this.
    """
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await delete_department(conn, user, department_id)
    

@app.post("/roles/create", response_model=RoleCreateResponse, tags=["Roles"])
async def roles_create(req: RoleCreateRequest, user=Depends(get_current_user)):
    """
    Create a new user role.
    Only Admin or HR can access this endpoint.
    """
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await create_role(conn, user, req)

@app.get("/roles/list", tags=["Roles"])
async def roles_list(user=Depends(get_current_user)):
    """
    Get all roles.
    Only Admin and HR can access this endpoint.
    """
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return {"roles": await list_roles(conn, user)}

@app.put("/roles/update", response_model=RoleUpdateResponse, tags=["Roles"])
async def roles_update(req: RoleUpdateRequest, user=Depends(get_current_user)):
    """
    Update an existing role.
    Only Admin or HR can access this endpoint.
    """
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await update_role(conn, user, req)

@app.delete("/roles/{role_id}", tags=["Roles"])
async def roles_delete(role_id: str, user=Depends(get_current_user)):
    """
    Delete an existing role.
    Only Admin or HR can access this endpoint.
    """
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await delete_role(conn, user, role_id)
    

@app.post("/auth/reset-password", response_model=ResetPasswordResponse, tags=["Auth"])
async def reset_password_api(req: ResetPasswordRequest):
    """
    Allow user to reset their password using email.
    """
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        return await reset_password(conn, req)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))  # Render provides PORT env var
    uvicorn.run(app, host="0.0.0.0", port=port)