from fastapi import HTTPException
from typing import Optional

async def get_leave_balance(conn, user, employee_id: Optional[str] = None):
    """
    Get leave balances for the current user or for a specific employee (if HR/Admin/Manager).
    """

    # Get caller role
    role = await conn.fetchval("""
        SELECT r.name 
        FROM roles r
        JOIN users u ON u.role_id = r.id
        WHERE u.id=$1
    """, user["id"])

    # Only HR/Admin/Manager can view others' balances
    if employee_id and role not in ("hr", "admin", "line_manager"):
        raise HTTPException(status_code=403, detail="Not authorized to view others’ leave balance")

    # Default: employee sees their own
    target_id = employee_id or user["id"]

    # Fetch leave balances
    rows = await conn.fetch("""
        SELECT 
            lb.user_id,
            u.email,
            lt.name AS leave_type,
            lb.total_entitled,
            lb.used_days,
            lb.carried_forward,
            lb.remaining
        FROM leave_balance lb
        JOIN leave_types lt ON lb.leave_type_id = lt.id
        JOIN users u ON lb.user_id = u.id
        WHERE lb.user_id = $1
        ORDER BY lt.name
    """, target_id)

    if not rows:
        raise HTTPException(status_code=404, detail="No leave balance found for this user")

    result = {
        "user_id": str(rows[0]["user_id"]),
        "email": rows[0]["email"],
        "balances": [
            {
                "leave_type": r["leave_type"],
                "total_entitled": float(r["total_entitled"]),
                "used_days": float(r["used_days"]),
                "carried_forward": float(r["carried_forward"]),
                "remaining": float(r["remaining"]),
            }
            for r in rows
        ]
    }
    return result
