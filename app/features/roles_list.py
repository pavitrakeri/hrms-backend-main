from fastapi import HTTPException

async def list_roles(conn, user):
    """
    List all roles.
    Accessible to Admin and HR only.
    """
    
    # Check permission
    role = await conn.fetchval("""
        SELECT r.name FROM roles r
        JOIN users u ON u.role_id = r.id
        WHERE u.id=$1
    """, user["id"])

    if role not in ("admin", "hr"):
        raise HTTPException(status_code=403, detail="Not authorized to view roles")

    # Fetch all roles
    rows = await conn.fetch("""
        SELECT id, name, description
        FROM roles
        ORDER BY name ASC
    """)

    return [
        {
            "id": str(row["id"]),
            "name": row["name"],
            "description": row["description"]
        }
        for row in rows
    ]
