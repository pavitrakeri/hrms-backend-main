from fastapi import HTTPException

async def list_departments(conn, user):
    """
    List all departments.
    Accessible to Admin, HR, and managers.
    """
    
    # Check permission
    role = await conn.fetchval("""
        SELECT r.name FROM roles r
        JOIN users u ON u.role_id = r.id
        WHERE u.id=$1
    """, user["id"])

    if role not in ("admin", "hr", "line_manager"):
        raise HTTPException(status_code=403, detail="Not authorized to view departments")

    # Fetch all departments with manager and HR details
    rows = await conn.fetch("""
        SELECT 
            d.id,
            d.name,
            d.manager_id,
            m.full_name as manager_name,
            m.email as manager_email,
            d.hr_id,
            h.full_name as hr_name,
            h.email as hr_email,
            d.created_at
        FROM departments d
        LEFT JOIN users m ON d.manager_id = m.id
        LEFT JOIN users h ON d.hr_id = h.id
        ORDER BY d.name ASC
    """)

    return [
        {
            "id": str(row["id"]),
            "name": row["name"],
            "manager_id": str(row["manager_id"]) if row["manager_id"] else None,
            "manager_name": row["manager_name"],
            "manager_email": row["manager_email"],
            "hr_id": str(row["hr_id"]) if row["hr_id"] else None,
            "hr_name": row["hr_name"],
            "hr_email": row["hr_email"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None
        }
        for row in rows
    ]
