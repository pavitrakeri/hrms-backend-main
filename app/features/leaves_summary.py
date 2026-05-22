from fastapi import HTTPException
from typing import Optional

async def get_leave_summary(conn, user, employee_id: Optional[str] = None):
    """
    Get summary of sick and annual leaves for current user or another employee.
    Returns totals, used, and remaining values for both.
    """

    # Get caller role
    role = await conn.fetchval("""
        SELECT r.name 
        FROM roles r
        JOIN users u ON u.role_id = r.id
        WHERE u.id=$1
    """, user["id"])

    # Restrict access
    if employee_id and role not in ("hr", "admin", "line_manager"):
        raise HTTPException(status_code=403, detail="Not authorized to view others’ leave summary")

    target_id = employee_id or user["id"]

    # Fetch leave balances for this user
    rows = await conn.fetch("""
        SELECT 
            lt.name AS leave_type,
            lb.total_entitled,
            lb.used_days,
            lb.remaining,
            u.email,
            lb.user_id
        FROM leave_balance lb
        JOIN leave_types lt ON lb.leave_type_id = lt.id
        JOIN users u ON lb.user_id = u.id
        WHERE lb.user_id=$1
    """, target_id)

    if not rows:
        raise HTTPException(status_code=404, detail="No leave balances found")

    # Initialize default values
    summary = {
        "total_sick_leave": 0,
        "sick_leave_used": 0,
        "remaining_sick_leave": 0,
        "total_annual_leave": 0,
        "annual_leave_used": 0,
        "remaining_annual_leave": 0,
        "email": rows[0]["email"],
        "user_id": str(rows[0]["user_id"])
    }

    # Aggregate values
    for r in rows:
        lt = r["leave_type"].lower()
        if lt == "sick":
            summary["total_sick_leave"] = float(r["total_entitled"])
            summary["sick_leave_used"] = float(r["used_days"])
            summary["remaining_sick_leave"] = float(r["remaining"])
        elif lt == "annual":
            summary["total_annual_leave"] = float(r["total_entitled"])
            summary["annual_leave_used"] = float(r["used_days"])
            summary["remaining_annual_leave"] = float(r["remaining"])

    return summary
