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

    fields_set = getattr(req, "model_fields_set", getattr(req, "__fields_set__", None))

    # ✅ 3. Resolve Name
    name = req.name if (fields_set and "name" in fields_set and req.name is not None) else dept["name"]

    # ✅ 4. Resolve Manager (optional, support explicit clearing)
    if fields_set and "manager_email" in fields_set:
        if req.manager_email:
            manager_row = await conn.fetchrow("SELECT id FROM users WHERE email=$1", req.manager_email)
            if not manager_row:
                raise HTTPException(status_code=400, detail="Manager not found")
            manager_id = manager_row["id"]
        else:
            manager_id = None
    else:
        manager_id = dept["manager_id"]

    # ✅ 5. Resolve HR (optional, support explicit clearing)
    if fields_set and "hr_email" in fields_set:
        if req.hr_email:
            hr_row = await conn.fetchrow("SELECT id FROM users WHERE email=$1", req.hr_email)
            if not hr_row:
                raise HTTPException(status_code=400, detail="HR not found")
            hr_id = hr_row["id"]
        else:
            hr_id = None
    else:
        hr_id = dept["hr_id"]

    # ✅ 6. Update Department
    await conn.execute("""
        UPDATE departments
        SET 
            name = $1,
            manager_id = $2,
            hr_id = $3
        WHERE id = $4
    """, name, manager_id, hr_id, req.department_id)

    return {
        "status": "success",
        "message": f"Department updated successfully",
        "department_id": req.department_id
    }


async def delete_department(conn, user, department_id: str):
    """
    Delete an existing department.
    Only Admin or HR can perform this action.
    """
    # 1. Check permission
    role = await conn.fetchval("""
        SELECT r.name FROM roles r
        JOIN users u ON u.role_id = r.id
        WHERE u.id=$1
    """, user["id"])

    if role not in ("admin", "hr"):
        raise HTTPException(status_code=403, detail="Only Admin or HR can delete departments")

    # 2. Check if department exists
    dept = await conn.fetchrow("SELECT name FROM departments WHERE id=$1", department_id)
    if not dept:
        raise HTTPException(status_code=404, detail="Department not found")

    # 3. Check if there are users in this department
    users_count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE department_id=$1", department_id)
    if users_count > 0:
        raise HTTPException(status_code=400, detail="Cannot delete department. There are employees assigned to it.")

    # 4. Check if there are projects in this department
    projects_count = await conn.fetchval("SELECT COUNT(*) FROM projects WHERE department_id=$1", department_id)
    if projects_count > 0:
        raise HTTPException(status_code=400, detail="Cannot delete department. There are projects assigned to it.")

    # 5. Delete Department
    await conn.execute("DELETE FROM departments WHERE id=$1", department_id)

    return {
        "status": "success",
        "message": f"Department '{dept['name']}' deleted successfully"
    }