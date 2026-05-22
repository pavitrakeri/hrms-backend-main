import os
import uuid
from fastapi import HTTPException, UploadFile
from datetime import datetime
from app.features.notifications import create_db_notification
from app.config import supabase, SUPABASE_BUCKET
import tempfile


async def get_my_policies(conn, user):
    rows = await conn.fetch("""
        SELECT p.id as policy_id, p.title, p.description, p.file_url,
               pa.acknowledged, pa.acknowledged_at
        FROM policy_assignments pa
        JOIN policies p ON pa.policy_id = p.id
        WHERE pa.user_id=$1
        ORDER BY p.created_at DESC
    """, user["id"])

    return [
        {
            "policy_id": str(r["policy_id"]),
            "title": r["title"],
            "description": r["description"],
            "file_url": r["file_url"],
            "acknowledged": r["acknowledged"],
            "acknowledged_at": r["acknowledged_at"].isoformat() if r["acknowledged_at"] else None
        }
        for r in rows
    ]

async def acknowledge_policy(conn, user, policy_id: str):
    assignment = await conn.fetchrow("""
        SELECT id, acknowledged FROM policy_assignments
        WHERE policy_id=$1 AND user_id=$2
    """, policy_id, user["id"])
    if not assignment:
        raise HTTPException(status_code=404, detail="Policy not assigned to you")

    if assignment["acknowledged"]:
        return {"status": "success", "message": "Already acknowledged", "policy_id": policy_id}

    await conn.execute("""
        UPDATE policy_assignments
        SET acknowledged=true, acknowledged_at=now()
        WHERE id=$1
    """, assignment["id"])

    return {"status": "success", "message": "Policy acknowledged", "policy_id": policy_id}



async def upload_policy(conn, user, title: str, description: str, file: UploadFile, roles: list, departments: list):
    """
    HR/Admin uploads a policy document to Supabase and assigns it to users based on role/department.
    - Files are stored under hrms-docs/policies/<uuid>.<ext>
    """
    # 1️⃣ Verify uploader role
    caller_role = await conn.fetchval("""
        SELECT r.name FROM roles r
        JOIN users u ON u.role_id = r.id
        WHERE u.id=$1
    """, user["id"])

    if caller_role not in ("hr", "admin"):
        raise HTTPException(status_code=403, detail="Only HR or Admin can upload policies")

    # 2️⃣ Prepare file details
    ext = file.filename.split(".")[-1]
    file_path = f"policies/{uuid.uuid4()}.{ext}"

    try:
        # Read the file content directly
        file_bytes = await file.read()

        # Upload file to Supabase (hrms-docs/policies/<uuid>.<ext>)
        upload_res = supabase.storage.from_(SUPABASE_BUCKET).upload(file_path, file_bytes)

        if hasattr(upload_res, "error") and upload_res.error is not None:
            raise HTTPException(status_code=500, detail=f"Supabase upload failed: {upload_res.error.message}")
        # Get a **public URL** for now (can later be switched to signed URL)
        file_url = supabase.storage.from_(SUPABASE_BUCKET).get_public_url(file_path)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"File upload failed: {str(e)}")

    # 3️⃣ Insert into policies table
    row = await conn.fetchrow("""
        INSERT INTO policies (id, title, description, file_url)
        VALUES (gen_random_uuid(), $1, $2, $3)
        RETURNING id
    """, title, description, file_url)
    policy_id = str(row["id"])

    # 4️⃣ Find users to assign
    base_query = """
        SELECT u.id, u.email FROM users u
        JOIN roles r ON u.role_id = r.id
        WHERE u.is_active=true
    """
    conditions, params = [], []

    if roles:
        conditions.append("r.name = ANY($1)")
        params.append(roles)

    if departments:
        conditions.append(f"u.department_name = ANY(${len(params)+1})")
        params.append(departments)

    if conditions:
        base_query += " AND " + " AND ".join(conditions)

    employees = await conn.fetch(base_query, *params)

    # 5️⃣ Assign to users + notify
    assigned_count = 0
    for emp in employees:
        await conn.execute("""
            INSERT INTO policy_assignments (id, policy_id, user_id)
            VALUES (gen_random_uuid(), $1, $2)
        """, policy_id, emp["id"])

        await create_db_notification(conn, str(emp["id"]), "policy_assigned", {"policy_id": policy_id})
        assigned_count += 1

    return {
        "status": "success",
        "message": "Policy uploaded and assigned successfully",
        "policy_id": policy_id,
        "assigned_count": assigned_count,
        "file_url": file_url
    }