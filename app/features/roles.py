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
        INSERT INTO roles (id, name, description)
        VALUES (gen_random_uuid(), $1, $2)
        RETURNING id
    """, req.name, req.description)

    return {
        "status": "success",
        "message": f"Role '{req.name}' created successfully",
        "role_id": str(row["id"])
    }


async def update_role(conn, user, req):
    """
    Update an existing role.
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
        raise HTTPException(status_code=403, detail="Only Admin or HR can update roles")

    # ✅ 2. Check if role exists
    role_row = await conn.fetchrow("SELECT * FROM roles WHERE id=$1", req.role_id)
    if not role_row:
        raise HTTPException(status_code=404, detail="Role not found")

    fields_set = getattr(req, "model_fields_set", getattr(req, "__fields_set__", None))

    # ✅ 3. Resolve fields
    name = req.name if (fields_set and "name" in fields_set and req.name is not None) else role_row["name"]
    description = req.description if (fields_set and "description" in fields_set) else role_row["description"]

    # ✅ 4. Check name uniqueness if changed
    if name.lower() != role_row["name"].lower():
        existing = await conn.fetchrow("SELECT id FROM roles WHERE LOWER(name)=LOWER($1) AND id!=$2", name, req.role_id)
        if existing:
            raise HTTPException(status_code=400, detail=f"Role '{name}' already exists")

    # ✅ 5. Update Role
    await conn.execute("""
        UPDATE roles
        SET name = $1, description = $2
        WHERE id = $3
    """, name, description, req.role_id)

    return {
        "status": "success",
        "message": "Role updated successfully",
        "role_id": req.role_id
    }


async def delete_role(conn, user, role_id: str):
    """
    Delete an existing role.
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
        raise HTTPException(status_code=403, detail="Only Admin or HR can delete roles")

    # ✅ 2. Check if role exists
    role_row = await conn.fetchrow("SELECT name FROM roles WHERE id=$1", role_id)
    if not role_row:
        raise HTTPException(status_code=404, detail="Role not found")

    # ✅ 3. Protect default system roles
    if role_row["name"].lower() in ("admin", "hr", "employee", "line_manager", "cfo"):
        raise HTTPException(status_code=400, detail="Cannot delete default system roles")

    # ✅ 4. Check if any users are assigned this role
    users_count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE role_id=$1", role_id)
    if users_count > 0:
        raise HTTPException(status_code=400, detail="Cannot delete role. There are users assigned to it.")

    # ✅ 5. Delete Role
    await conn.execute("DELETE FROM roles WHERE id=$1", role_id)

    return {
        "status": "success",
        "message": f"Role '{role_row['name']}' deleted successfully"
    }
