from fastapi import HTTPException

async def create_role(conn, user, req):
    """
    Create a new role.
    Only Admin or HR can perform this action.
    """

    # ✅ 1. Check permission
    role = await conn.fetchval("""
        SELECT r.name
        FROM roles r
        JOIN users u ON u.role_id = r.id
        WHERE u.id = $1
    """, user["id"])

    if role not in ("admin", "hr"):
        raise HTTPException(status_code=403, detail="Only Admin or HR can create roles")

    # ✅ 2. Check if role already exists
    existing = await conn.fetchrow("SELECT id FROM roles WHERE LOWER(name) = LOWER($1)", req.name)
    if existing:
        raise HTTPException(status_code=400, detail=f"Role '{req.name}' already exists")

    # ✅ 3. Insert new role
    row = await conn.fetchrow("""
        INSERT INTO roles (id, name)
        VALUES (gen_random_uuid(), $1)
        RETURNING id
    """, req.name)

    return {
        "status": "success",
        "message": f"Role '{req.name}' created successfully",
        "role_id": str(row["id"])
    }
