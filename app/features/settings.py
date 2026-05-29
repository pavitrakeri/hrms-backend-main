from fastapi import HTTPException
import bcrypt
import logging
from typing import Dict, Any

async def get_my_profile(conn, user: Dict[str, Any]):
    """
    Fetch the detailed profile of the currently logged-in user.
    """
    row = await conn.fetchrow("""
        SELECT 
            u.id, u.email, u.full_name, r.name as role, 
            COALESCE(d.name, 'Unassigned') as department, 
            m.email as manager_email, u.is_active, u.created_at,
            u.status, u.office_location, u.designation, u.joining_date, u.employment_status,
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
    """, user["id"])

    if not row:
        raise HTTPException(status_code=404, detail="Profile not found")

    return {
        "id": str(row["id"]),
        "email": row["email"],
        "full_name": row["full_name"],
        "role": row["role"],
        "department": row["department"],
        "manager_email": row["manager_email"],
        "is_active": row["is_active"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "status": row["status"],
        "office_location": row["office_location"],
        "designation": row["designation"],
        "joining_date": row["joining_date"].isoformat() if row["joining_date"] else None,
        "employment_status": row["employment_status"],
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
        "emergency_contact_number": row["emergency_contact_number"]
    }

async def update_my_profile(conn, user: Dict[str, Any], req):
    """
    Update editable personal information for the currently logged-in user.
    """
    await conn.execute("""
        UPDATE users SET
            personal_email=$1,
            contact_number=$2,
            marital_status=$3,
            emergency_contact_name=$4,
            emergency_contact_number=$5,
            bank_account_number=$6
        WHERE id=$7
    """, 
        req.personal_email, 
        req.contact_number, 
        req.marital_status, 
        req.emergency_contact_name, 
        req.emergency_contact_number, 
        req.bank_account_number,
        user["id"]
    )
    return {"status": "success", "message": "Personal profile updated successfully"}

async def change_my_password(conn, user: Dict[str, Any], req):
    """
    Verify current password and set a new password for the current user.
    """
    # 1. Verify new password match
    if req.new_password != req.confirm_password:
        raise HTTPException(status_code=400, detail="New passwords do not match")

    # 2. Fetch current password hash
    row = await conn.fetchrow("SELECT password_hash FROM users WHERE id=$1", user["id"])
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    password_hash = row["password_hash"]

    # 3. Verify current password
    if not bcrypt.checkpw(req.current_password.encode("utf-8"), password_hash.encode("utf-8")):
        raise HTTPException(status_code=400, detail="Incorrect current password")

    # 4. Hash and save the new password
    new_pw_hash = bcrypt.hashpw(req.new_password.encode("utf-8"), bcrypt.gensalt()).decode()
    await conn.execute("UPDATE users SET password_hash=$1, password_reset_required=false WHERE id=$2", new_pw_hash, user["id"])

    return {"status": "success", "message": "Password changed successfully"}

async def get_company_settings(conn):
    """
    Get the global company configuration.
    """
    row = await conn.fetchrow("""
        SELECT company_name, office_start_time, office_end_time, weekend_days, currency
        FROM company_settings
        LIMIT 1
    """)
    if not row:
        # Fallback in case table is empty
        return {
            "company_name": "Aimploy",
            "office_start_time": "09:00:00",
            "office_end_time": "18:00:00",
            "weekend_days": "Saturday,Sunday",
            "currency": "AED"
        }
    
    # Format TIME objects to strings (HH:MM:SS) if they are datetime.time objects
    start_time = row["office_start_time"].isoformat() if hasattr(row["office_start_time"], "isoformat") else str(row["office_start_time"])
    end_time = row["office_end_time"].isoformat() if hasattr(row["office_end_time"], "isoformat") else str(row["office_end_time"])

    return {
        "company_name": row["company_name"],
        "office_start_time": start_time,
        "office_end_time": end_time,
        "weekend_days": row["weekend_days"],
        "currency": row["currency"]
    }

async def update_company_settings(conn, user: Dict[str, Any], req):
    """
    Update company configuration (Admin/HR only).
    """
    if user["role"] not in ("admin", "hr"):
        raise HTTPException(status_code=403, detail="Only HR or Admin can change company settings")

    # Check if a row exists
    exists = await conn.fetchval("SELECT 1 FROM company_settings LIMIT 1")
    if not exists:
        await conn.execute("""
            INSERT INTO company_settings (company_name, office_start_time, office_end_time, weekend_days, currency)
            VALUES ($1, $2::TIME, $3::TIME, $4, $5)
        """, req.company_name, req.office_start_time, req.office_end_time, req.weekend_days, req.currency)
    else:
        await conn.execute("""
            UPDATE company_settings SET
                company_name=$1,
                office_start_time=$2::TIME,
                office_end_time=$3::TIME,
                weekend_days=$4,
                currency=$5,
                updated_at=now()
        """, req.company_name, req.office_start_time, req.office_end_time, req.weekend_days, req.currency)

    return {"status": "success", "message": "Company settings updated successfully"}
