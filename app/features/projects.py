from fastapi import HTTPException
from typing import Optional, List
from datetime import date


async def _get_user_role(conn, user_id: str) -> str:
    role = await conn.fetchval("""
        SELECT r.name FROM roles r
        JOIN users u ON u.role_id = r.id
        WHERE u.id = $1
    """, user_id)
    return (role or "").lower()


def _is_admin_or_hr(role: str) -> bool:
    return role in ("admin", "hr")


async def _is_project_owner(conn, project_id: str, user_id: str) -> bool:
    row = await conn.fetchrow("""
        SELECT 1 FROM project_members
        WHERE project_id = $1 AND user_id = $2 AND role = 'owner'
    """, project_id, user_id)
    return row is not None


async def _can_manage_project(conn, user, project_id: str) -> bool:
    role = await _get_user_role(conn, user["id"])
    if _is_admin_or_hr(role):
        return True
    return await _is_project_owner(conn, project_id, user["id"])


async def _can_view_project(conn, user, project_id: str) -> bool:
    role = await _get_user_role(conn, user["id"])
    if _is_admin_or_hr(role):
        return True
    row = await conn.fetchrow("""
        SELECT 1 FROM project_members
        WHERE project_id = $1 AND user_id = $2
    """, project_id, user["id"])
    return row is not None


def _format_project_row(row) -> dict:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "description": row["description"],
        "created_by": str(row["created_by"]),
        "creator_email": row.get("creator_email"),
        "creator_name": row.get("creator_name"),
        "department_id": str(row["department_id"]) if row.get("department_id") else None,
        "department_name": row.get("department_name"),
        "start_date": row["start_date"].isoformat() if row.get("start_date") else None,
        "deadline": row["deadline"].isoformat() if row.get("deadline") else None,
        "assigned_to": row.get("assigned_to"),
        "status": row["status"],
        "task_count": int(row.get("task_count") or 0),
        "open_task_count": int(row.get("open_task_count") or 0),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


def _validate_dates(start_date: Optional[date], deadline: Optional[date]):
    if start_date and deadline and deadline < start_date:
        raise HTTPException(status_code=400, detail="Deadline cannot be before start date")


async def _add_project_members(conn, project_id: str, owner_id: str, member_ids: Optional[List[str]]):
    await conn.execute("""
        INSERT INTO project_members (id, project_id, user_id, role)
        VALUES (gen_random_uuid(), $1, $2, 'owner')
        ON CONFLICT (project_id, user_id) DO NOTHING
    """, project_id, owner_id)

    if not member_ids:
        return

    for member_id in member_ids:
        if member_id == owner_id:
            continue
        user = await conn.fetchrow(
            "SELECT id FROM users WHERE id = $1 AND is_active = true", member_id
        )
        if not user:
            raise HTTPException(status_code=400, detail=f"User not found: {member_id}")
        await conn.execute("""
            INSERT INTO project_members (id, project_id, user_id, role)
            VALUES (gen_random_uuid(), $1, $2, 'member')
            ON CONFLICT (project_id, user_id) DO NOTHING
        """, project_id, member_id)


_PROJECT_SELECT = """
    SELECT
        p.id, p.name, p.description, p.created_by, p.department_id, p.status,
        p.start_date, p.deadline,
        p.created_at, p.updated_at,
        u.email AS creator_email,
        u.full_name AS creator_name,
        d.name AS department_name,
        (
            SELECT STRING_AGG(u2.full_name, ', ' ORDER BY u2.full_name)
            FROM project_members pm2
            JOIN users u2 ON u2.id = pm2.user_id
            WHERE pm2.project_id = p.id AND pm2.role = 'member'
        ) AS assigned_to,
        COUNT(t.id)::int AS task_count,
        COUNT(t.id) FILTER (WHERE t.status != 'done')::int AS open_task_count
"""


async def create_project(conn, user, req):
    _validate_dates(req.start_date, req.deadline)

    dept_id = req.department_id
    if not dept_id:
        dept_id = await conn.fetchval(
            "SELECT department_id FROM users WHERE id = $1", user["id"]
        )

    async with conn.transaction():
        row = await conn.fetchrow("""
            INSERT INTO projects (
                id, name, description, created_by, department_id,
                start_date, deadline, status
            )
            VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6, 'active')
            RETURNING id
        """, req.name, req.description, user["id"], dept_id, req.start_date, req.deadline)

        project_id = str(row["id"])
        await _add_project_members(conn, project_id, user["id"], req.member_ids)

    return {
        "status": "success",
        "message": "Project created",
        "project_id": project_id,
    }


async def list_projects(conn, user, include_archived: bool = False):
    role = await _get_user_role(conn, user["id"])
    status_filter = "" if include_archived else "AND p.status = 'active'"

    if not _is_admin_or_hr(role):
        rows = await conn.fetch(f"""
            {_PROJECT_SELECT}
            FROM projects p
            JOIN users u ON u.id = p.created_by
            LEFT JOIN departments d ON d.id = p.department_id
            LEFT JOIN tasks t ON t.project_id = p.id
            WHERE 1=1 {status_filter}
              AND EXISTS (SELECT 1 FROM project_members pm WHERE pm.project_id = p.id AND pm.user_id = $1)
            GROUP BY p.id, u.email, u.full_name, d.name
            ORDER BY p.updated_at DESC
        """, user["id"])
    else:
        rows = await conn.fetch(f"""
            {_PROJECT_SELECT}
            FROM projects p
            JOIN users u ON u.id = p.created_by
            LEFT JOIN departments d ON d.id = p.department_id
            LEFT JOIN tasks t ON t.project_id = p.id
            WHERE 1=1 {status_filter}
            GROUP BY p.id, u.email, u.full_name, d.name
            ORDER BY p.updated_at DESC
        """)

    return {"projects": [_format_project_row(r) for r in rows]}


async def get_project_detail(conn, user, project_id: str):
    if not await _can_view_project(conn, user, project_id):
        raise HTTPException(status_code=403, detail="Not allowed to view this project")

    row = await conn.fetchrow(f"""
        {_PROJECT_SELECT}
        FROM projects p
        JOIN users u ON u.id = p.created_by
        LEFT JOIN departments d ON d.id = p.department_id
        LEFT JOIN tasks t ON t.project_id = p.id
        WHERE p.id = $1
        GROUP BY p.id, u.email, u.full_name, d.name
    """, project_id)

    if not row:
        raise HTTPException(status_code=404, detail="Project not found")

    members = await conn.fetch("""
        SELECT pm.user_id, pm.role, u.email, u.full_name
        FROM project_members pm
        JOIN users u ON u.id = pm.user_id
        WHERE pm.project_id = $1
        ORDER BY pm.role DESC, u.full_name
    """, project_id)

    return {
        "project": _format_project_row(row),
        "members": [
            {
                "user_id": str(m["user_id"]),
                "email": m["email"],
                "full_name": m["full_name"],
                "role": m["role"],
            }
            for m in members
        ],
    }


async def update_project(conn, user, project_id: str, req):
    if not await _can_manage_project(conn, user, project_id):
        raise HTTPException(status_code=403, detail="Not allowed to update this project")

    existing = await conn.fetchrow("SELECT id FROM projects WHERE id = $1", project_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Project not found")

    updates = []
    values = []
    idx = 1

    if req.name is not None:
        updates.append(f"name = ${idx}")
        values.append(req.name)
        idx += 1
    if req.description is not None:
        updates.append(f"description = ${idx}")
        values.append(req.description)
        idx += 1
    if req.start_date is not None:
        updates.append(f"start_date = ${idx}")
        values.append(req.start_date)
        idx += 1
    if req.deadline is not None:
        updates.append(f"deadline = ${idx}")
        values.append(req.deadline)
        idx += 1

    if req.start_date is not None or req.deadline is not None:
        current = await conn.fetchrow(
            "SELECT start_date, deadline FROM projects WHERE id = $1", project_id
        )
        new_start = req.start_date if req.start_date is not None else current["start_date"]
        new_deadline = req.deadline if req.deadline is not None else current["deadline"]
        _validate_dates(new_start, new_deadline)

    if not updates and req.member_ids is None:
        raise HTTPException(status_code=400, detail="No fields to update")

    if updates:
        updates.append("updated_at = now()")
        values.append(project_id)
        await conn.execute(
            f"UPDATE projects SET {', '.join(updates)} WHERE id = ${idx}",
            *values,
        )

    if req.member_ids is not None:
        await conn.execute("""
            DELETE FROM project_members
            WHERE project_id = $1 AND role = 'member'
        """, project_id)
        owner_id = await conn.fetchval(
            "SELECT created_by FROM projects WHERE id = $1", project_id
        )
        await _add_project_members(conn, project_id, str(owner_id), req.member_ids)

    return await get_project_detail(conn, user, project_id)


async def archive_project(conn, user, project_id: str):
    if not await _can_manage_project(conn, user, project_id):
        raise HTTPException(status_code=403, detail="Not allowed to archive this project")

    result = await conn.execute("""
        UPDATE projects SET status = 'archived', updated_at = now()
        WHERE id = $1 AND status = 'active'
    """, project_id)

    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Project not found or already archived")

    return {"status": "success", "message": "Project archived"}
