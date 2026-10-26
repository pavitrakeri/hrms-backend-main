from typing import Optional, List, Dict, Any
from datetime import date, datetime, timezone
from fastapi import HTTPException
import uuid

async def get_latest_attendance(
    conn,
    user: Dict[str, Any],
    limit: int = 10,
    employee_id: Optional[str] = None,
    scope: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Attendance records query.
    - If scope == 'me' or employee role: always returns the logged-in user's own records.
    - If employee_id is passed and caller has role admin/hr/line_manager: returns that employee's records.
    - If admin or hr and no scope/employee_id specified: returns company-wide/department-wide recent records.
    """
    user_id_str = str(user["id"])
    role = (user.get("role") or "").lower().strip()
    if not role:
        # Fallback query from db
        role_name = await conn.fetchval("""
            SELECT LOWER(r.name)
            FROM roles r
            JOIN users u ON u.role_id = r.id
            WHERE u.id = $1::uuid
        """, user_id_str)
        role = role_name or "employee"

    base_query = """
        SELECT a.id, a.user_id, u.email as employee_email, u.full_name as employee_name,
               a.clock_in_at, a.clock_out_at, a.total_seconds, a.location,
               COALESCE(a.is_regularised, false) as is_regularised, a.created_at
        FROM attendance a
        JOIN users u ON a.user_id = u.id
    """
    params = []

    # If personal scope requested or employee role: strictly own records
    if scope == "me" or role == "employee" or (employee_id and str(employee_id) == user_id_str):
        base_query += " WHERE a.user_id = $1::uuid ORDER BY a.clock_in_at DESC NULLS LAST LIMIT $2"
        params = [user_id_str, limit]

    elif role == "line_manager":
        if employee_id:
            belongs = await conn.fetchval(
                "SELECT 1 FROM users WHERE id = $1::uuid AND manager_id = $2::uuid",
                employee_id, user_id_str
            )
            if not belongs and employee_id != user_id_str:
                raise HTTPException(status_code=403, detail="Employee not under this manager")
            base_query += " WHERE a.user_id = $1::uuid ORDER BY a.clock_in_at DESC NULLS LAST LIMIT $2"
            params = [employee_id, limit]
        else:
            base_query += " WHERE (u.manager_id = $1::uuid OR a.user_id = $1::uuid) ORDER BY a.clock_in_at DESC NULLS LAST LIMIT $2"
            params = [user_id_str, limit]

    elif role == "hr":
        if employee_id:
            base_query += " WHERE a.user_id = $1::uuid ORDER BY a.clock_in_at DESC NULLS LAST LIMIT $2"
            params = [employee_id, limit]
        else:
            dept_id = await conn.fetchval("SELECT department_id FROM users WHERE id = $1::uuid", user_id_str)
            if dept_id:
                base_query += " WHERE u.department_id = $1::uuid ORDER BY a.clock_in_at DESC NULLS LAST LIMIT $2"
                params = [dept_id, limit]
            else:
                base_query += " ORDER BY a.clock_in_at DESC NULLS LAST LIMIT $1"
                params = [limit]

    elif role == "admin":
        if employee_id:
            base_query += " WHERE a.user_id = $1::uuid ORDER BY a.clock_in_at DESC NULLS LAST LIMIT $2"
            params = [employee_id, limit]
        else:
            base_query += " ORDER BY a.clock_in_at DESC NULLS LAST LIMIT $1"
            params = [limit]
    else:
        # Fallback to own records
        base_query += " WHERE a.user_id = $1::uuid ORDER BY a.clock_in_at DESC NULLS LAST LIMIT $2"
        params = [user_id_str, limit]

    rows = await conn.fetch(base_query, *params)

    return [
        {
            "id": str(r["id"]),
            "user_id": str(r["user_id"]),
            "employee_email": r["employee_email"],
            "employee_name": r["employee_name"],
            "clock_in_at": r["clock_in_at"].isoformat() if r["clock_in_at"] else None,
            "clock_out_at": r["clock_out_at"].isoformat() if r["clock_out_at"] else None,
            "total_seconds": r["total_seconds"],
            "location": r["location"],
            "is_regularised": bool(r["is_regularised"]),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]


async def get_attendance_report(
    conn,
    user: Dict[str, Any],
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    employee_id: Optional[str] = None,
    department_id: Optional[str] = None,
    status: Optional[str] = None,  # "all" | "completed" | "in_progress" | "regularised"
    search: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
) -> Dict[str, Any]:
    """
    Comprehensive attendance report module for Admin and HR.
    """
    role = (user.get("role") or "").lower().strip()
    perms = user.get("permissions") or []
    if role not in ("admin", "hr") and "manage_attendance" not in perms and "manage_employees" not in perms:
        raise HTTPException(
            status_code=403,
            detail="Access denied. Attendance report is restricted to Admin and HR users.",
        )

    conditions = ["1=1"]
    params = []
    param_idx = 1

    if from_date:
        conditions.append(f"a.clock_in_at::date >= ${param_idx}")
        params.append(from_date)
        param_idx += 1

    if to_date:
        conditions.append(f"a.clock_in_at::date <= ${param_idx}")
        params.append(to_date)
        param_idx += 1

    if employee_id:
        conditions.append(f"a.user_id = ${param_idx}::uuid")
        params.append(employee_id)
        param_idx += 1

    if department_id:
        conditions.append(f"u.department_id = ${param_idx}::uuid")
        params.append(department_id)
        param_idx += 1

    if status and status != "all":
        if status == "completed":
            conditions.append("a.clock_out_at IS NOT NULL")
        elif status == "in_progress":
            conditions.append("a.clock_out_at IS NULL")
        elif status == "regularised":
            conditions.append("COALESCE(a.is_regularised, false) = true")

    if search and search.strip():
        term = f"%{search.strip()}%"
        conditions.append(f"(u.full_name ILIKE ${param_idx} OR u.email ILIKE ${param_idx})")
        params.append(term)
        param_idx += 1

    where_clause = " AND ".join(conditions)

    # Count total matching rows
    count_query = f"""
        SELECT COUNT(*)
        FROM attendance a
        JOIN users u ON a.user_id = u.id
        LEFT JOIN departments d ON u.department_id = d.id
        WHERE {where_clause}
    """
    total_count = await conn.fetchval(count_query, *params)

    # Fetch rows with pagination
    data_query = f"""
        SELECT a.id, a.user_id, u.full_name as employee_name, u.email as employee_email,
               u.department_id, d.name as department_name,
               a.clock_in_at, a.clock_out_at, a.total_seconds, a.location,
               COALESCE(a.is_regularised, false) as is_regularised, a.created_at
        FROM attendance a
        JOIN users u ON a.user_id = u.id
        LEFT JOIN departments d ON u.department_id = d.id
        WHERE {where_clause}
        ORDER BY a.clock_in_at DESC NULLS LAST
        LIMIT ${param_idx} OFFSET ${param_idx + 1}
    """
    rows = await conn.fetch(data_query, *params, limit, offset)

    # Compute summary aggregates
    completed_sessions = 0
    in_progress_sessions = 0
    total_seconds = 0
    unique_employees = set()

    formatted_records = []
    for r in rows:
        cid = r["clock_in_at"]
        cod = r["clock_out_at"]
        t_sec = r["total_seconds"] or 0
        is_comp = cod is not None
        if is_comp:
            completed_sessions += 1
            total_seconds += t_sec
        else:
            in_progress_sessions += 1

        unique_employees.add(str(r["user_id"]))

        date_str = cid.strftime("%Y-%m-%d") if cid else (r["created_at"].strftime("%Y-%m-%d") if r["created_at"] else "—")

        formatted_records.append({
            "id": str(r["id"]),
            "user_id": str(r["user_id"]),
            "employee_name": r["employee_name"] or "Unknown",
            "employee_email": r["employee_email"],
            "department_id": str(r["department_id"]) if r["department_id"] else None,
            "department_name": r["department_name"] or "General",
            "date": date_str,
            "clock_in_at": cid.isoformat() if cid else None,
            "clock_out_at": cod.isoformat() if cod else None,
            "total_seconds": t_sec,
            "location": r["location"] or "Standard Punch",
            "is_regularised": bool(r["is_regularised"]),
            "status": "completed" if is_comp else "in_progress",
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        })

    average_seconds = (total_seconds // completed_sessions) if completed_sessions > 0 else 0

    pending_regularisations = await conn.fetchval(
        "SELECT COUNT(*) FROM attendance_regularisations WHERE status = 'pending'"
    ) or 0

    summary = {
        "total_records": total_count,
        "completed_sessions": completed_sessions,
        "in_progress_sessions": in_progress_sessions,
        "total_seconds": total_seconds,
        "average_seconds": average_seconds,
        "unique_employees_present": len(unique_employees),
        "pending_regularisations": pending_regularisations,
    }

    return {
        "summary": summary,
        "records": formatted_records,
        "total_count": total_count,
    }


# =========================================================================
# ATTENDANCE REGULARISATION
# =========================================================================

async def apply_attendance_regularisation(
    conn,
    user: Dict[str, Any],
    req: Any,
) -> Dict[str, Any]:
    """
    Employee submits an attendance regularisation request.
    """
    user_id_str = str(user["id"])

    # Ensure times are timezone-aware
    cin = req.requested_clock_in
    cout = req.requested_clock_out
    if cin.tzinfo is None:
        cin = cin.replace(tzinfo=timezone.utc)
    if cout.tzinfo is None:
        cout = cout.replace(tzinfo=timezone.utc)

    if cout <= cin:
        raise HTTPException(
            status_code=400,
            detail="Requested clock-out time must be after requested clock-in time.",
        )

    # Check for existing pending request for this user on the same date
    existing = await conn.fetchval("""
        SELECT id FROM attendance_regularisations
        WHERE user_id = $1::uuid AND request_date = $2 AND status = 'pending'
    """, user_id_str, req.request_date)

    if existing:
        raise HTTPException(
            status_code=400,
            detail="You already have a pending regularisation request for this date.",
        )

    # Insert request
    reg_id = await conn.fetchval("""
        INSERT INTO attendance_regularisations (
            user_id, attendance_id, request_date, requested_clock_in, requested_clock_out, reason, status
        )
        VALUES ($1::uuid, $2, $3, $4, $5, $6, 'pending')
        RETURNING id
    """,
        user_id_str,
        uuid.UUID(req.attendance_id) if req.attendance_id else None,
        req.request_date,
        cin,
        cout,
        req.reason.strip(),
    )

    # Create notifications for Admin and HR users
    try:
        from app.features.notifications import create_db_notification
        admin_and_hrs = await conn.fetch("""
            SELECT u.id FROM users u
            JOIN roles r ON u.role_id = r.id
            WHERE LOWER(r.name) IN ('admin', 'hr')
        """)
        for admin in admin_and_hrs:
            await create_db_notification(
                conn,
                str(admin["id"]),
                "attendance_regularisation_submitted",
                {
                    "regularisation_id": str(reg_id),
                    "employee_name": user.get("full_name") or user.get("email"),
                    "request_date": str(req.request_date),
                    "reason": req.reason.strip(),
                },
            )
    except Exception as e:
        print(f"[apply_attendance_regularisation] Notification warning: {e}")

    return {
        "status": "success",
        "message": "Attendance regularisation request submitted successfully. Awaiting approval.",
        "id": str(reg_id),
    }


async def get_my_regularisations(
    conn,
    user: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Get personal regularisation requests for the logged-in user.
    """
    user_id_str = str(user["id"])
    rows = await conn.fetch("""
        SELECT ar.id, ar.user_id, ar.request_date, ar.requested_clock_in, ar.requested_clock_out,
               ar.reason, ar.status, ar.attendance_id, ar.rejection_reason, ar.reviewed_at, ar.created_at,
               u.full_name as employee_name, u.email as employee_email,
               rev.full_name as reviewer_name
        FROM attendance_regularisations ar
        JOIN users u ON ar.user_id = u.id
        LEFT JOIN users rev ON ar.reviewed_by = rev.id
        WHERE ar.user_id = $1::uuid
        ORDER BY ar.created_at DESC
    """, user_id_str)

    return [
        {
            "id": str(r["id"]),
            "user_id": str(r["user_id"]),
            "employee_name": r["employee_name"] or "Employee",
            "employee_email": r["employee_email"],
            "request_date": str(r["request_date"]),
            "requested_clock_in": r["requested_clock_in"].isoformat() if r["requested_clock_in"] else "",
            "requested_clock_out": r["requested_clock_out"].isoformat() if r["requested_clock_out"] else "",
            "reason": r["reason"],
            "status": r["status"],
            "attendance_id": str(r["attendance_id"]) if r["attendance_id"] else None,
            "rejection_reason": r["rejection_reason"],
            "reviewer_name": r["reviewer_name"],
            "reviewed_at": r["reviewed_at"].isoformat() if r["reviewed_at"] else None,
            "created_at": r["created_at"].isoformat() if r["created_at"] else "",
        }
        for r in rows
    ]


async def get_all_regularisations(
    conn,
    user: Dict[str, Any],
    status: Optional[str] = None,
    department_id: Optional[str] = None,
    employee_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Get all regularisation requests for Admin/HR review.
    """
    role = (user.get("role") or "").lower().strip()
    perms = user.get("permissions") or []
    if role not in ("admin", "hr", "line_manager") and "manage_attendance" not in perms:
        raise HTTPException(status_code=403, detail="Access denied")

    conditions = ["1=1"]
    params = []
    idx = 1

    if status and status != "all":
        conditions.append(f"ar.status = ${idx}")
        params.append(status)
        idx += 1

    if department_id:
        conditions.append(f"u.department_id = ${idx}::uuid")
        params.append(department_id)
        idx += 1

    if employee_id:
        conditions.append(f"ar.user_id = ${idx}::uuid")
        params.append(employee_id)
        idx += 1

    if role == "line_manager":
        conditions.append(f"u.manager_id = ${idx}::uuid")
        params.append(str(user["id"]))
        idx += 1

    where_clause = " AND ".join(conditions)

    query = f"""
        SELECT ar.id, ar.user_id, ar.request_date, ar.requested_clock_in, ar.requested_clock_out,
               ar.reason, ar.status, ar.attendance_id, ar.rejection_reason, ar.reviewed_at, ar.created_at,
               u.full_name as employee_name, u.email as employee_email,
               d.name as department_name,
               rev.full_name as reviewer_name
        FROM attendance_regularisations ar
        JOIN users u ON ar.user_id = u.id
        LEFT JOIN departments d ON u.department_id = d.id
        LEFT JOIN users rev ON ar.reviewed_by = rev.id
        WHERE {where_clause}
        ORDER BY ar.created_at DESC
    """
    rows = await conn.fetch(query, *params)

    return [
        {
            "id": str(r["id"]),
            "user_id": str(r["user_id"]),
            "employee_name": r["employee_name"] or "Employee",
            "employee_email": r["employee_email"],
            "department_name": r["department_name"] or "General",
            "request_date": str(r["request_date"]),
            "requested_clock_in": r["requested_clock_in"].isoformat() if r["requested_clock_in"] else "",
            "requested_clock_out": r["requested_clock_out"].isoformat() if r["requested_clock_out"] else "",
            "reason": r["reason"],
            "status": r["status"],
            "attendance_id": str(r["attendance_id"]) if r["attendance_id"] else None,
            "rejection_reason": r["rejection_reason"],
            "reviewer_name": r["reviewer_name"],
            "reviewed_at": r["reviewed_at"].isoformat() if r["reviewed_at"] else None,
            "created_at": r["created_at"].isoformat() if r["created_at"] else "",
        }
        for r in rows
    ]


async def action_attendance_regularisation(
    conn,
    user: Dict[str, Any],
    reg_id: str,
    action: str,  # "approve" | "reject"
    rejection_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Approve or reject an attendance regularisation request.
    If approved, updates existing attendance session or creates a new regularised attendance entry.
    """
    role = (user.get("role") or "").lower().strip()
    perms = user.get("permissions") or []
    if role not in ("admin", "hr", "line_manager") and "manage_attendance" not in perms:
        raise HTTPException(status_code=403, detail="Access denied to review regularisation requests")

    reviewer_id = str(user["id"])

    reg = await conn.fetchrow("""
        SELECT * FROM attendance_regularisations WHERE id = $1::uuid
    """, reg_id)

    if not reg:
        raise HTTPException(status_code=404, detail="Regularisation request not found")

    if reg["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"Request is already {reg['status']}")

    now_utc = datetime.now(timezone.utc)

    if action == "approve":
        cin = reg["requested_clock_in"]
        cout = reg["requested_clock_out"]
        if cin.tzinfo is None:
            cin = cin.replace(tzinfo=timezone.utc)
        if cout.tzinfo is None:
            cout = cout.replace(tzinfo=timezone.utc)

        total_seconds = max(0, int((cout - cin).total_seconds()))

        # If linked to an existing attendance record, update it
        att_id = reg["attendance_id"]
        if att_id:
            await conn.execute("""
                UPDATE attendance
                SET clock_in_at = $1, clock_out_at = $2, total_seconds = $3,
                    is_regularised = true, location = COALESCE(location, 'Regularised Entry')
                WHERE id = $4::uuid
            """, cin, cout, total_seconds, str(att_id))
        else:
            # Check if there is an existing attendance record on that date
            existing_att = await conn.fetchrow("""
                SELECT id FROM attendance
                WHERE user_id = $1::uuid AND clock_in_at::date = $2
                ORDER BY clock_in_at DESC LIMIT 1
            """, reg["user_id"], reg["request_date"])

            if existing_att:
                await conn.execute("""
                    UPDATE attendance
                    SET clock_in_at = $1, clock_out_at = $2, total_seconds = $3, is_regularised = true
                    WHERE id = $4
                """, cin, cout, total_seconds, existing_att["id"])
            else:
                # Insert brand new attendance record
                new_att_id = await conn.fetchval("""
                    INSERT INTO attendance (
                        id, user_id, clock_in_at, clock_out_at, total_seconds, is_regularised, location
                    )
                    VALUES (gen_random_uuid(), $1, $2, $3, $4, true, 'Regularised Entry')
                    RETURNING id
                """, reg["user_id"], cin, cout, total_seconds)

                await conn.execute("""
                    UPDATE attendance_regularisations
                    SET attendance_id = $1
                    WHERE id = $2::uuid
                """, new_att_id, reg_id)

        # Update regularisation status to approved
        await conn.execute("""
            UPDATE attendance_regularisations
            SET status = 'approved', reviewed_by = $1::uuid, reviewed_at = $2
            WHERE id = $3::uuid
        """, reviewer_id, now_utc, reg_id)

        # Notify applicant
        try:
            from app.features.notifications import create_db_notification
            await create_db_notification(
                conn,
                str(reg["user_id"]),
                "attendance_regularisation_approved",
                {
                    "regularisation_id": reg_id,
                    "request_date": str(reg["request_date"]),
                    "message": "Your attendance regularisation request was approved.",
                },
            )
        except Exception as e:
            print(f"[action_attendance_regularisation] Notification error: {e}")

        return {
            "status": "success",
            "message": "Attendance regularisation approved successfully and attendance record updated.",
        }

    elif action == "reject":
        reason = (rejection_reason or "Rejected by supervisor").strip()
        await conn.execute("""
            UPDATE attendance_regularisations
            SET status = 'rejected', rejection_reason = $1, reviewed_by = $2::uuid, reviewed_at = $3
            WHERE id = $4::uuid
        """, reason, reviewer_id, now_utc, reg_id)

        # Notify applicant
        try:
            from app.features.notifications import create_db_notification
            await create_db_notification(
                conn,
                str(reg["user_id"]),
                "attendance_regularisation_rejected",
                {
                    "regularisation_id": reg_id,
                    "request_date": str(reg["request_date"]),
                    "rejection_reason": reason,
                    "message": f"Your attendance regularisation request was rejected: {reason}",
                },
            )
        except Exception as e:
            print(f"[action_attendance_regularisation] Notification error: {e}")

        return {
            "status": "success",
            "message": "Attendance regularisation request rejected.",
        }
    else:
        raise HTTPException(status_code=400, detail="Action must be 'approve' or 'reject'")
