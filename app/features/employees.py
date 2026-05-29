import bcrypt
import logging
import os
from fastapi import HTTPException, BackgroundTasks
from typing import Optional, List
from app.features.notifications import send_email_background

async def add_employee(conn, user, req, bg: BackgroundTasks = None):
    """
    HR/Admin can add new employees with extended details.
    """
    try:
        # Validate caller role
        caller_role = await conn.fetchval("""
            SELECT r.name FROM roles r
            JOIN users u ON u.role_id = r.id
            WHERE u.id=$1
        """, user["id"])
        if caller_role not in ("hr", "admin"):
            raise HTTPException(status_code=403, detail="Only HR or Admin can add employees")

        # Resolve role_id
        role_row = await conn.fetchrow("SELECT id FROM roles WHERE name=$1", req.role)
        if not role_row:
            raise HTTPException(status_code=400, detail="Invalid role")
        role_id = role_row["id"]

        # Resolve manager
        manager_id = None
        if req.manager_email:
            manager = await conn.fetchrow("SELECT id FROM users WHERE email=$1", req.manager_email)
            if not manager:
                raise HTTPException(status_code=400, detail="Manager not found")
            manager_id = manager["id"]

        # Resolve HR
        hr_id = None
        if req.hr_email:
            hr = await conn.fetchrow("SELECT id FROM users WHERE email=$1", req.hr_email)
            if not hr:
                raise HTTPException(status_code=400, detail="HR not found")
            hr_id = hr["id"]

        # Resolve or create department
        dept_id = None
        if req.department:
            dept = await conn.fetchrow("SELECT id, name FROM departments WHERE name=$1", req.department)
            if not dept:
                dept = await conn.fetchrow("""
                    INSERT INTO departments (id, name, manager_id, hr_id)
                    VALUES (gen_random_uuid(), $1, $2, $3)
                    RETURNING id, name
                """, req.department, manager_id, hr_id)
            dept_id = dept["id"]

        # Hash password
        pw_hash = bcrypt.hashpw(req.password.encode("utf-8"), bcrypt.gensalt()).decode()

        # Insert new employee with all extended fields
        row = await conn.fetchrow("""
                INSERT INTO users (
                    id, email, full_name, password_hash, role_id,
                    manager_id, is_active, created_at,
                    department_id, joining_date,
                    status, office_location, designation,
                    gender, date_of_birth, marital_status, nationality,
                    passport_number, emirates_id_number, uid_number, file_number,
                    contract_type, labour_card_number, labour_card_expiry,
                    visa_sponsorship, residence_visa_expiry, work_email,
                    contact_number, personal_email, basic_salary, hra, mobile,
                    transportation, other, total_salary, flight_ticket,
                    wps_unique_id, wps, medical_insurance_category,
                    aadhaar_card_number, pan_card_number, pf_account_number,
                    esi_number, bank_account_number, ifsc_code,
                    emergency_contact_name, emergency_contact_number,
                    password_reset_required
                )
                VALUES (
                    gen_random_uuid(), $1, $2, $3, $4,
                    $5, true, now(),
                    $6, $7,
                    $8, $9, $10,
                    $11, $12, $13, $14,
                    $15, $16, $17, $18,
                    $19, $20, $21,
                    $22, $23, $24,
                    $25, $26, $27, $28, $29,
                    $30, $31, $32, $33,
                    $34, $35, $36,
                    $37, $38, $39, $40, $41, $42,
                    $43, $44,
                    true
                )
                RETURNING id
            """,
            req.email, req.full_name, pw_hash, role_id,
            manager_id,  # $5
            dept_id, req.joining_date,  # $6–$7
            req.status, req.office_location, req.designation,  # $8-$10
            req.gender, req.date_of_birth, req.marital_status, req.nationality,  # $11–$14
            req.passport_number, req.emirates_id_number, req.uid_number, req.file_number,  # $15–$18
            req.contract_type, req.labour_card_number, req.labour_card_expiry,  # $19–$21
            req.visa_sponsorship, req.residence_visa_expiry, req.work_email,  # $22–$24
            req.contact_number, req.personal_email, req.basic_salary, req.hra, req.mobile,  # $25–$29
            req.transportation, req.other, req.total_salary, req.flight_ticket,  # $30–$33
            req.wps_unique_id, req.wps, req.medical_insurance_category,  # $34–$36
            req.aadhaar_card_number, req.pan_card_number, req.pf_account_number,  # $37-$39
            req.esi_number, req.bank_account_number, req.ifsc_code,  # $40-$42
            req.emergency_contact_name, req.emergency_contact_number  # $43-$44
            )

        # Send welcome email notification
        frontend_url = os.getenv("FRONTEND_URL", "https://hrms.aimploy.org").rstrip("/")
        login_url = f"{frontend_url}/login"
        
        subject = "Welcome to Aimploy HRMS - Your Account Details"
        body = (
            f"Hello {req.full_name},\n\n"
            f"Your account has been successfully created on the Aimploy HRMS portal.\n\n"
            f"You can log in to your account using the following credentials:\n"
            f"Portal URL: {login_url}\n"
            f"Username (Email): {req.email}\n"
            f"Temporary Password: {req.password}\n\n"
            f"Please log in and update your password as soon as possible.\n\n"
            f"Best regards,\n"
            f"Aimploy HR Team"
        )
        
        send_email_background(bg, req.email, subject, body)

        return {"status": "success", "message": "Employee created", "employee_id": str(row["id"])}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Employee creation failed: {str(e)}")


async def list_employees(conn, user, department: Optional[str] = None, role: Optional[str] = None, active: Optional[bool] = None):
    """
    HR/Admin see all, Manager sees only their team.
    """
    # get caller role
    caller_role = await conn.fetchval("""
        SELECT r.name FROM roles r
        JOIN users u ON u.role_id = r.id
        WHERE u.id=$1
    """, user["id"])

    if caller_role not in ("hr", "admin", "line_manager", "cfo"):
        raise HTTPException(status_code=403, detail="Not authorized to view employee list")

    # base query with proper department join
    query = """
        SELECT u.id, u.email, u.full_name, r.name as role, 
               COALESCE(d.name, 'Unassigned') as department, 
               m.email as manager_email, u.is_active, u.created_at
        FROM users u
        JOIN roles r ON u.role_id = r.id
        LEFT JOIN departments d ON u.department_id = d.id
        LEFT JOIN users m ON u.manager_id = m.id
        WHERE 1=1
    """
    params = []
    conditions = []

    if caller_role == "line_manager":
        # restrict to same department or direct reports
        dept_id = await conn.fetchval("SELECT department_id FROM users WHERE id=$1", user["id"])
        query += " AND (u.department_id=$1 OR u.manager_id=$1)"
        params.append(user["id"])
        # NOTE: If you want manager to only see their department, replace with dept_id logic.

    if department:
        conditions.append(f"d.name=$${len(params)+1}$$")
        params.append(department)

    if role:
        conditions.append(f"r.name=$${len(params)+1}$$")
        params.append(role)

    if active is not None:
        conditions.append(f"u.is_active=$${len(params)+1}$$")
        params.append(active)

    if conditions:
        query += " AND " + " AND ".join(conditions)

    query += " ORDER BY u.created_at DESC"

    rows = await conn.fetch(query, *params)

    return [
        {
            "id": str(r["id"]),
            "email": r["email"],
            "full_name": r["full_name"],
            "role": r["role"],
            "department": r["department"],
            "manager_email": r["manager_email"],
            "is_active": r["is_active"],
            "created_at": r["created_at"].isoformat()
        }
        for r in rows
    ]


async def get_employee_details(conn, user, employee_id: str):
    """
    Get detailed information about a specific employee.
    """
    # Log request for debugging (shows in server logs)
    try:
        caller_id = user.get("id") if isinstance(user, dict) else None
    except Exception:
        caller_id = None
    logging.info("get_employee_details called: caller_id=%s employee_id=%s", caller_id, employee_id)

    # Check authorization
    caller_role = await conn.fetchval("""
        SELECT r.name FROM roles r
        JOIN users u ON u.role_id = r.id
        WHERE u.id=$1
    """, user["id"])

    if caller_role not in ("hr", "admin", "line_manager", "cfo"):
        raise HTTPException(status_code=403, detail="Not authorized to view employee details")

    # Fetch employee with full details
    row = await conn.fetchrow("""
        SELECT 
            u.id, u.email, u.full_name, r.name as role, 
            COALESCE(d.name, 'Unassigned') as department, 
            m.email as manager_email, u.is_active, u.created_at,
            u.status, u.office_location, u.designation,
            u.gender, u.date_of_birth, u.marital_status, u.nationality,
            u.passport_number, u.emirates_id_number, u.uid_number, u.file_number,
            u.contract_type, u.labour_card_number, u.labour_card_expiry,
            u.visa_sponsorship, u.residence_visa_expiry, u.work_email,
            u.contact_number, u.personal_email, u.basic_salary, u.hra, u.mobile,
            u.transportation, u.other, u.total_salary, u.flight_ticket,
            u.wps_unique_id, u.wps, u.medical_insurance_category,
            u.aadhaar_card_number, u.pan_card_number, u.pf_account_number,
            u.esi_number, u.bank_account_number, u.ifsc_code,
            u.emergency_contact_name, u.emergency_contact_number
        FROM users u
        JOIN roles r ON u.role_id = r.id
        LEFT JOIN departments d ON u.department_id = d.id
        LEFT JOIN users m ON u.manager_id = m.id
        WHERE u.id=$1
    """, employee_id)

    if not row:
        logging.info("employee not found: employee_id=%s requested_by=%s", employee_id, caller_id)
        raise HTTPException(status_code=404, detail="Employee not found")

    return {
        "id": str(row["id"]),
        "email": row["email"],
        "full_name": row["full_name"],
        "role": row["role"],
        "department": row["department"],
        "manager_email": row["manager_email"],
        "is_active": row["is_active"],
        "created_at": row["created_at"].isoformat(),
        "status": row["status"],
        "office_location": row["office_location"],
        "designation": row["designation"],
        "gender": row["gender"],
        "date_of_birth": row["date_of_birth"].isoformat() if row["date_of_birth"] else None,
        "marital_status": row["marital_status"],
        "nationality": row["nationality"],
        "passport_number": row["passport_number"],
        "emirates_id_number": row["emirates_id_number"],
        "uid_number": row["uid_number"],
        "file_number": row["file_number"],
        "contract_type": row["contract_type"],
        "labour_card_number": row["labour_card_number"],
        "labour_card_expiry": row["labour_card_expiry"].isoformat() if row["labour_card_expiry"] else None,
        "visa_sponsorship": row["visa_sponsorship"],
        "residence_visa_expiry": row["residence_visa_expiry"].isoformat() if row["residence_visa_expiry"] else None,
        "work_email": row["work_email"],
        "contact_number": row["contact_number"],
        "personal_email": row["personal_email"],
        "basic_salary": float(row["basic_salary"]) if row["basic_salary"] else None,
        "hra": float(row["hra"]) if row["hra"] else None,
        "mobile": float(row["mobile"]) if row["mobile"] else None,
        "transportation": float(row["transportation"]) if row["transportation"] else None,
        "other": float(row["other"]) if row["other"] else None,
        "total_salary": float(row["total_salary"]) if row["total_salary"] else None,
        "flight_ticket": row["flight_ticket"],
        "wps_unique_id": row["wps_unique_id"],
        "wps": row["wps"],
        "medical_insurance_category": row["medical_insurance_category"],
        "aadhaar_card_number": row["aadhaar_card_number"],
        "pan_card_number": row["pan_card_number"],
        "pf_account_number": row["pf_account_number"],
        "esi_number": row["esi_number"],
        "bank_account_number": row["bank_account_number"],
        "ifsc_code": row["ifsc_code"],
        "emergency_contact_name": row["emergency_contact_name"],
        "emergency_contact_number": row["emergency_contact_number"],
        "employee_documents": []  # Placeholder for documents
    }


async def update_employee(conn, user, employee_id: str, req):
    """
    HR/Admin can update employee details.
    """
    try:
        # Validate caller role
        caller_role = await conn.fetchval("""
            SELECT r.name FROM roles r
            JOIN users u ON u.role_id = r.id
            WHERE u.id=$1
        """, user["id"])
        if caller_role not in ("hr", "admin"):
            raise HTTPException(status_code=403, detail="Only HR or Admin can edit employee details")

        # Resolve role_id
        role_row = await conn.fetchrow("SELECT id FROM roles WHERE name=$1", req.role)
        if not role_row:
            raise HTTPException(status_code=400, detail="Invalid role")
        role_id = role_row["id"]

        # Resolve manager
        manager_id = None
        if req.manager_email:
            manager = await conn.fetchrow("SELECT id FROM users WHERE email=$1", req.manager_email)
            if not manager:
                raise HTTPException(status_code=400, detail="Manager not found")
            manager_id = manager["id"]

        # Resolve HR
        hr_id = None
        if req.hr_email:
            hr = await conn.fetchrow("SELECT id FROM users WHERE email=$1", req.hr_email)
            if not hr:
                raise HTTPException(status_code=400, detail="HR not found")
            hr_id = hr["id"]

        # Resolve or create department
        dept_id = None
        if req.department:
            dept = await conn.fetchrow("SELECT id, name FROM departments WHERE name=$1", req.department)
            if not dept:
                dept = await conn.fetchrow("""
                    INSERT INTO departments (id, name, manager_id, hr_id)
                    VALUES (gen_random_uuid(), $1, $2, $3)
                    RETURNING id, name
                """, req.department, manager_id, hr_id)
            dept_id = dept["id"]

        # Update query
        await conn.execute("""
            UPDATE users SET
                email=$1, full_name=$2, role_id=$3, manager_id=$4,
                department_id=$5, joining_date=$6, status=$7, office_location=$8,
                designation=$9, gender=$10, date_of_birth=$11, marital_status=$12,
                nationality=$13, passport_number=$14, emirates_id_number=$15,
                uid_number=$16, file_number=$17, contract_type=$18,
                labour_card_number=$19, labour_card_expiry=$20, visa_sponsorship=$21,
                residence_visa_expiry=$22, work_email=$23, contact_number=$24,
                personal_email=$25, basic_salary=$26, hra=$27, mobile=$28,
                transportation=$29, other=$30, total_salary=$31, flight_ticket=$32,
                wps_unique_id=$33, wps=$34, medical_insurance_category=$35,
                is_active=$36,
                aadhaar_card_number=$37, pan_card_number=$38, pf_account_number=$39,
                esi_number=$40, bank_account_number=$41, ifsc_code=$42,
                emergency_contact_name=$43, emergency_contact_number=$44
            WHERE id=$45
        """,
            req.email, req.full_name, role_id, manager_id,
            dept_id, req.joining_date, req.status, req.office_location,
            req.designation, req.gender, req.date_of_birth, req.marital_status,
            req.nationality, req.passport_number, req.emirates_id_number,
            req.uid_number, req.file_number, req.contract_type,
            req.labour_card_number, req.labour_card_expiry, req.visa_sponsorship,
            req.residence_visa_expiry, req.work_email, req.contact_number,
            req.personal_email, req.basic_salary, req.hra, req.mobile,
            req.transportation, req.other, req.total_salary, req.flight_ticket,
            req.wps_unique_id, req.wps, req.medical_insurance_category,
            req.is_active,
            req.aadhaar_card_number, req.pan_card_number, req.pf_account_number,
            req.esi_number, req.bank_account_number, req.ifsc_code,
            req.emergency_contact_name, req.emergency_contact_number,
            employee_id
        )

        # Update password if provided
        if getattr(req, "password", None):
            pw_hash = bcrypt.hashpw(req.password.encode("utf-8"), bcrypt.gensalt()).decode()
            await conn.execute("UPDATE users SET password_hash=$1, password_reset_required=true WHERE id=$2", pw_hash, employee_id)

        return {"status": "success", "message": "Employee details updated"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Employee update failed: {str(e)}")

