from typing import Optional
from fastapi import HTTPException

async def get_latest_attendance(conn, user, limit: int = 10, employee_id: Optional[str] = None):
    """
    Role-based attendance access:
    - Employee: only self
    - Manager: their direct reports
    - HR: all employees in their department
    - Admin: all employees
    Optional: employee_id filter for HR/Admin/Manager (within scope).
    """
    role = await conn.fetchval("""
        SELECT r.name 
        FROM roles r
        JOIN users u ON u.role_id = r.id
        WHERE u.id=$1
    """, user["id"])

    base_query = """
        SELECT a.id, a.user_id, u.email as employee_email,
               a.clock_in_at, a.clock_out_at, a.total_seconds, a.created_at
        FROM attendance a
        JOIN users u ON a.user_id = u.id
    """
    params = []

    if role == "employee":
        base_query += " WHERE a.user_id=$1 ORDER BY a.clock_in_at DESC NULLS LAST LIMIT $2"
        params = [user["id"], limit]

    elif role == "line_manager":
        if employee_id:
            # check that employee belongs to manager
            belongs = await conn.fetchval("SELECT 1 FROM users WHERE id=$1 AND manager_id=$2", employee_id, user["id"])
            if not belongs:
                raise HTTPException(status_code=403, detail="Employee not under this manager")
            base_query += " WHERE a.user_id=$1 ORDER BY a.clock_in_at DESC NULLS LAST LIMIT $2"
            params = [employee_id, limit]
        else:
            base_query += " WHERE u.manager_id=$1 ORDER BY a.clock_in_at DESC NULLS LAST LIMIT $2"
            params = [user["id"], limit]

    elif role == "hr":
        dept_id = await conn.fetchval("SELECT department_id FROM users WHERE id=$1", user["id"])
        if employee_id:
            belongs = await conn.fetchval("SELECT 1 FROM users WHERE id=$1 AND department_id=$2", employee_id, dept_id)
            if not belongs:
                raise HTTPException(status_code=403, detail="Employee not in this HR's department")
            base_query += " WHERE a.user_id=$1 ORDER BY a.clock_in_at DESC NULLS LAST LIMIT $2"
            params = [employee_id, limit]
        else:
            base_query += " WHERE u.department_id=$1 ORDER BY a.clock_in_at DESC NULLS LAST LIMIT $2"
            params = [dept_id, limit]

    elif role == "admin":
        if employee_id:
            base_query += " WHERE a.user_id=$1 ORDER BY a.clock_in_at DESC NULLS LAST LIMIT $2"
            params = [employee_id, limit]
        else:
            base_query += " ORDER BY a.clock_in_at DESC NULLS LAST LIMIT $1"
            params = [limit]

    rows = await conn.fetch(base_query, *params)

    return [
        {
            "id": str(r["id"]),
            "user_id": str(r["user_id"]),
            "employee_email": r["employee_email"],
            "clock_in_at": r["clock_in_at"].isoformat() if r["clock_in_at"] else None,
            "clock_out_at": r["clock_out_at"].isoformat() if r["clock_out_at"] else None,
            "total_seconds": r["total_seconds"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None
        }
        for r in rows
    ]
