from fastapi import HTTPException
from app.features.notifications import create_db_notification
import uuid

async def create_department(conn, user, req):
    """
    Create a new department
    Only Admin or HR can perform this action
    """
    # check if the caller has permission 
    role = await conn.fetchval("""
            SELECT r.name from roles r 
            JOIN users u ON u.role_id = r.id
            WHERE u.id=$1
            """,user["id"])
            
    if role not in ("admin", "hr"):
        raise HTTPException(status_code = 403, detail = "Only Admin or HR can create permision")
    
    # check if department already exists

    existing = await conn.fetchrow(""" SELECT id FROM departments 
                                   WHERE LOWER(name) = LOWER($1)
                                   """, req.name)
    
    if existing:
        raise HTTPException(status_code = 400, detail = f"Department {req.name} already exists")
    
    # resolve manager 
    manager_id = None
    if req.manager_email:
        manager_row = await conn.fetchrow("SELECT id FROM users WHERE email = $1 ", req.manager_email)
        if not manager_row:
            raise HTTPException(status_code=400, detail = "Manager not found")
        manager_id = manager_row["id"]

    # resolve hr
    hr_id = None
    if req.hr_email:
        hr_row = await conn.fetchrow("SELECT id FROM users WHERE email = $1", req.hr_email)
        if not hr_row:
            raise HTTPException(status_code=400, detail = "HR not found")
        hr_id = hr_row["id"]


    # insert department
    row = await conn.fetchrow("""
            INSERT INTO departments (id, name, manager_id, hr_id)
            VALUES (gen_random_uuid(), $1, $2, $3)
            RETURNING id
            """, req.name, manager_id, hr_id)
    
    dept_id = str(row["id"])


    # notification to HR/Manager
    if manager_id:
        await create_db_notification(conn, manager_id, "department_assigned", {"department_id": dept_id})
    if hr_id:
        await create_db_notification(conn, hr_id, "department_assigned", {"department_id": dept_id})

    return {
        "status": "success",
        "message": f"Department '{req.name}' created successfully",
        "department_id": dept_id
    }


async def update_department(conn, user, req):
    """
    Update an existing department.
    Only Admin or HR can perform this action.
    """

    # ✅ 1. Check permission
    role = await conn.fetchval("""
        SELECT r.name FROM roles r
        JOIN users u ON u.role_id = r.id
        WHERE u.id=$1
    """, user["id"])

    if role not in ("admin", "hr"):
        raise HTTPException(status_code=403, detail="Only Admin or HR can update departments")

    # ✅ 2. Check if department exists
    dept = await conn.fetchrow("SELECT * FROM departments WHERE id=$1", req.department_id)
    if not dept:
        raise HTTPException(status_code=404, detail="Department not found")

    # ✅ 3. Resolve Manager (optional)
    manager_id = dept["manager_id"]
    if req.manager_email:
        manager_row = await conn.fetchrow("SELECT id FROM users WHERE email=$1", req.manager_email)
        if not manager_row:
            raise HTTPException(status_code=400, detail="Manager not found")
        manager_id = manager_row["id"]

    # ✅ 4. Resolve HR (optional)
    hr_id = dept["hr_id"]
    if req.hr_email:
        hr_row = await conn.fetchrow("SELECT id FROM users WHERE email=$1", req.hr_email)
        if not hr_row:
            raise HTTPException(status_code=400, detail="HR not found")
        hr_id = hr_row["id"]

    # ✅ 5. Update Department
    await conn.execute("""
        UPDATE departments
        SET 
            name = COALESCE($1, name),
            manager_id = COALESCE($2, manager_id),
            hr_id = COALESCE($3, hr_id),
            created_at = created_at  -- keep same timestamp
        WHERE id = $4
    """, req.name, manager_id, hr_id, req.department_id)

    return {
        "status": "success",
        "message": f"Department updated successfully",
        "department_id": req.department_id
    }