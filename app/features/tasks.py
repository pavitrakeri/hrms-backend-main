from fastapi import HTTPException, BackgroundTasks
from typing import Optional
from app.features.projects import _get_user_role, _is_admin_or_hr, _is_project_owner, _can_view_project
from app.features.notifications import create_db_notification

VALID_STATUSES = ("todo", "in_progress", "done")


def _format_task_row(row) -> dict:
    return {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "project_name": row.get("project_name"),
        "title": row["title"],
        "description": row["description"],
        "assignee_id": str(row["assignee_id"]) if row.get("assignee_id") else None,
        "assignee_email": row.get("assignee_email"),
        "assignee_name": row.get("assignee_name"),
        "created_by": str(row["created_by"]),
        "creator_email": row.get("creator_email"),
        "created_name": row.get("creator_name"),
        "start_date": row["start_date"].isoformat() if row.get("start_date") else None,
        "due_date": row["due_date"].isoformat() if row.get("due_date") else None,
        "status": row["status"],
        "timer_started_at": row["timer_started_at"].isoformat() if row.get("timer_started_at") else None,
        "time_spent_seconds": int(row.get("time_spent_seconds") or 0),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


async def _get_task_with_context(conn, task_id: str):
    row = await conn.fetchrow("""
        SELECT
            t.id, t.project_id, t.title, t.description, t.assignee_id,
            t.created_by, t.start_date, t.due_date, t.status, 
            t.timer_started_at, t.time_spent_seconds,
            t.created_at, t.updated_at,
            p.name AS project_name,
            au.email AS assignee_email,
            au.full_name AS assignee_name,
            cu.email AS creator_email,
            cu.full_name AS creator_name
        FROM tasks t
        JOIN projects p ON p.id = t.project_id
        JOIN users cu ON cu.id = t.created_by
        LEFT JOIN users au ON au.id = t.assignee_id
        WHERE t.id = $1
    """, task_id)
    return row


async def _can_edit_task(conn, user, task_row) -> bool:
    role = await _get_user_role(conn, user["id"])
    if _is_admin_or_hr(role):
        return True
    if str(task_row["created_by"]) == user["id"]:
        return True
    if task_row.get("assignee_id") and str(task_row["assignee_id"]) == user["id"]:
        return True
    if await _is_project_owner(conn, str(task_row["project_id"]), user["id"]):
        return True
    return False


async def _can_delete_task(conn, user, task_row) -> bool:
    role = await _get_user_role(conn, user["id"])
    if _is_admin_or_hr(role):
        return True
    if str(task_row["created_by"]) == user["id"]:
        return True
    if await _is_project_owner(conn, str(task_row["project_id"]), user["id"]):
        return True
    return False


async def _notify_assignee(conn, bg: Optional[BackgroundTasks], assignee_id: str, task_id: str, project_id: str):
    if not assignee_id:
        return
    await create_db_notification(
        conn,
        assignee_id,
        "task_assigned",
        {"task_id": task_id, "project_id": project_id},
    )


async def _ensure_project_active(conn, project_id: str):
    status = await conn.fetchval("SELECT status FROM projects WHERE id = $1", project_id)
    if not status:
        raise HTTPException(status_code=404, detail="Project not found")
    if status != "active":
        raise HTTPException(status_code=400, detail="Cannot modify tasks on an archived project")


async def create_task(conn, user, project_id: str, req, bg: Optional[BackgroundTasks] = None):
    await _ensure_project_active(conn, project_id)
    if not await _can_view_project(conn, user, project_id):
        raise HTTPException(status_code=403, detail="Not allowed to create tasks for this project")

    status = req.status or "todo"
    if status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status. Use one of: {VALID_STATUSES}")

    assignee_id = req.assignee_id or user["id"]

    assignee = await conn.fetchrow(
        "SELECT id FROM users WHERE id = $1 AND is_active = true", assignee_id
    )
    if not assignee:
        raise HTTPException(status_code=400, detail="Assignee not found")

    row = await conn.fetchrow("""
        INSERT INTO tasks (
            id, project_id, title, description, assignee_id, created_by, start_date, due_date, status
        )
        VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING id
    """, project_id, req.title, req.description, assignee_id, user["id"], req.start_date, req.due_date, status)

    task_id = str(row["id"])

    if assignee_id != user["id"]:
        await _notify_assignee(conn, bg, assignee_id, task_id, project_id)

    return {
        "status": "success",
        "message": "Task created",
        "task_id": task_id,
    }


async def list_project_tasks(conn, user, project_id: str, status: Optional[str] = None):
    project = await conn.fetchrow("SELECT id FROM projects WHERE id = $1", project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not await _can_view_project(conn, user, project_id):
        raise HTTPException(status_code=403, detail="Not allowed to view tasks for this project")

    if status and status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status. Use one of: {VALID_STATUSES}")

    query = """
        SELECT
            t.id, t.project_id, t.title, t.description, t.assignee_id,
            t.created_by, t.start_date, t.due_date, t.status,
            t.timer_started_at, t.time_spent_seconds,
            t.created_at, t.updated_at,
            p.name AS project_name,
            au.email AS assignee_email,
            au.full_name AS assignee_name,
            cu.email AS creator_email,
            cu.full_name AS creator_name
        FROM tasks t
        JOIN projects p ON p.id = t.project_id
        JOIN users cu ON cu.id = t.created_by
        LEFT JOIN users au ON au.id = t.assignee_id
        WHERE t.project_id = $1
    """
    params = [project_id]

    if status:
        query += " AND t.status = $2"
        params.append(status)

    query += " ORDER BY t.due_date NULLS LAST, t.created_at DESC"

    rows = await conn.fetch(query, *params)
    return {"tasks": [_format_task_row(r) for r in rows]}


async def get_my_tasks(conn, user, status: Optional[str] = None):
    if status and status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status. Use one of: {VALID_STATUSES}")

    query = """
        SELECT
            t.id, t.project_id, t.title, t.description, t.assignee_id,
            t.created_by, t.start_date, t.due_date, t.status,
            t.timer_started_at, t.time_spent_seconds,
            t.created_at, t.updated_at,
            p.name AS project_name,
            au.email AS assignee_email,
            au.full_name AS assignee_name,
            cu.email AS creator_email,
            cu.full_name AS creator_name
        FROM tasks t
        JOIN projects p ON p.id = t.project_id
        JOIN users cu ON cu.id = t.created_by
        LEFT JOIN users au ON au.id = t.assignee_id
        WHERE t.assignee_id = $1 AND p.status = 'active'
    """
    params = [user["id"]]

    if status:
        query += " AND t.status = $2"
        params.append(status)

    query += " ORDER BY t.due_date NULLS LAST, t.created_at DESC"

    rows = await conn.fetch(query, *params)
    return {"tasks": [_format_task_row(r) for r in rows]}


async def get_task(conn, user, task_id: str):
    row = await _get_task_with_context(conn, task_id)
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    if not await _can_view_project(conn, user, str(row["project_id"])):
        raise HTTPException(status_code=403, detail="Not allowed to view this task")
    return {"task": _format_task_row(row)}


async def update_task(conn, user, task_id: str, req, bg: Optional[BackgroundTasks] = None):
    row = await _get_task_with_context(conn, task_id)
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")

    if not await _can_edit_task(conn, user, row):
        raise HTTPException(status_code=403, detail="Not allowed to update this task")

    if req.status and req.status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status. Use one of: {VALID_STATUSES}")

    old_assignee = str(row["assignee_id"]) if row.get("assignee_id") else None

    if req.assignee_id is not None:
        assignee = await conn.fetchrow(
            "SELECT id FROM users WHERE id = $1 AND is_active = true", req.assignee_id
        )
        if not assignee:
            raise HTTPException(status_code=400, detail="Assignee not found")

    updates = []
    values = []
    idx = 1

    for field, val in [
        ("title", req.title),
        ("description", req.description),
        ("assignee_id", req.assignee_id),
        ("start_date", req.start_date),
        ("due_date", req.due_date),
        ("status", req.status),
    ]:
        if val is not None:
            updates.append(f"{field} = ${idx}")
            values.append(val)
            idx += 1

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    updates.append("updated_at = now()")
    values.append(task_id)

    await conn.execute(
        f"UPDATE tasks SET {', '.join(updates)} WHERE id = ${idx}",
        *values,
    )

    new_assignee = req.assignee_id if req.assignee_id is not None else old_assignee
    if req.assignee_id is not None and new_assignee != old_assignee and new_assignee:
        await _notify_assignee(conn, bg, new_assignee, task_id, str(row["project_id"]))

    return await get_task(conn, user, task_id)


async def update_task_status(conn, user, task_id: str, status: str, bg: Optional[BackgroundTasks] = None):
    from app.datamodels.project_models import TaskUpdateRequest
    return await update_task(conn, user, task_id, TaskUpdateRequest(status=status), bg)


async def toggle_task_timer(conn, user, task_id: str):
    row = await _get_task_with_context(conn, task_id)
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")

    if not await _can_edit_task(conn, user, row):
        raise HTTPException(status_code=403, detail="Not allowed to update this task")

    from datetime import datetime, timezone

    if row["timer_started_at"] is None:
        # Start the timer
        await conn.execute("""
            UPDATE tasks SET timer_started_at = $1, updated_at = now()
            WHERE id = $2
        """, datetime.now(timezone.utc), task_id)
    else:
        # Stop the timer
        started_at = row["timer_started_at"]
        now = datetime.now(timezone.utc)
        elapsed = int((now - started_at).total_seconds())
        await conn.execute("""
            UPDATE tasks SET 
                time_spent_seconds = COALESCE(time_spent_seconds, 0) + $1,
                timer_started_at = NULL,
                updated_at = now()
            WHERE id = $2
        """, elapsed, task_id)

    return await get_task(conn, user, task_id)


async def delete_task(conn, user, task_id: str):
    row = await _get_task_with_context(conn, task_id)
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")

    if not await _can_delete_task(conn, user, row):
        raise HTTPException(status_code=403, detail="Not allowed to delete this task")

    await conn.execute("DELETE FROM tasks WHERE id = $1", task_id)
    return {"status": "success", "message": "Task deleted"}
