# app/features/leaves.py
import json
import uuid
from typing import List, Optional
from fastapi import HTTPException, BackgroundTasks, UploadFile
from datetime import datetime, date, timedelta
from app.config import supabase, SUPABASE_BUCKET

from app.features.notifications import create_db_notification, send_email_background

# Map leave_type name -> list of approver roles (in order)
LEAVE_WORKFLOWS = {
    "sick": ["manager", "hr"],
    "casual": ["manager", "hr", "admin"],
}

async def _get_user_with_employment(conn, user_id: str):
    return await conn.fetchrow("""
        SELECT id, email, full_name, manager_id, department_id, employment_status, joining_date
        FROM users WHERE id=$1
    """, user_id)


def _requested_leave_days(start_date: date, end_date: date, half_day: bool, half_day_slot: Optional[str]) -> float:
    """Inclusive leave day count"""
    days = (end_date - start_date).days + 1
    if half_day:
        return max(0.5, days - 0.5)
    return float(days)


def _is_past_probation(user_row) -> bool:
    """Checks if user completed 6 months since joining or explicitly marked permanent."""
    status = user_row.get("employment_status", "").lower() if user_row else "probation"
    joining_date = user_row.get("joining_date")

    if status == "permanent":
        return True
    if status == "probation":
        if joining_date:
            try:
                return (date.today() - joining_date) >= timedelta(days=180)
            except Exception:
                return False
        return False
    return True


async def _get_leave_balance(conn, user_id: str, leave_type_id: int):
    """Fetch balance record for a given user + leave type."""
    return await conn.fetchrow("""
        SELECT id, total_entitled, used_days, carried_forward, remaining
        FROM leave_balance
        WHERE user_id=$1 AND leave_type_id=$2 AND year=EXTRACT(YEAR FROM now())
    """, user_id, leave_type_id)


async def _increment_used_days(conn, user_id: str, leave_type_id: int, used_delta: float):
    """Deduct from leave balance when leave is approved."""
    await conn.execute("""
        UPDATE leave_balance
        SET used_days = COALESCE(used_days, 0) + $1, last_updated=now()
        WHERE user_id=$2 AND leave_type_id=$3 AND year=EXTRACT(YEAR FROM now())
    """, used_delta, user_id, leave_type_id)

async def _get_user(conn, user_id: str):
    return await conn.fetchrow("SELECT id, email, full_name, manager_id, department_id FROM users WHERE id = $1", user_id)

async def _get_department_roles(conn, department_id: Optional[str], role: str):
    """
    role = 'manager' or 'hr'
    returns user_id for department role if set else None
    """
    if not department_id:
        return None
    if role == "manager":
        r = await conn.fetchrow("SELECT manager_id FROM departments WHERE id=$1", department_id)
        return r["manager_id"] if r else None
    if role == "hr":
        r = await conn.fetchrow("SELECT hr_id FROM departments WHERE id=$1", department_id)
        return r["hr_id"] if r else None
    return None

async def _get_global_by_role(conn, role_name: str):
    """Find one global user by role (e.g., admin). Returns row or None."""
    r = await conn.fetchrow("""
        SELECT u.id, u.email, u.full_name FROM users u
        JOIN roles r ON u.role_id = r.id
        WHERE r.name=$1 AND u.is_active=true
        ORDER BY created_at ASC LIMIT 1
    """, role_name)
    return r

async def _resolve_approvers_for_user(conn, applicant_user_row, workflow_roles: List[str]) -> List[dict]:
    """
    Given applicant row and workflow (role keys), resolve actual approver user rows.
    Returns list of {id, email, role_key}
    """
    approvers = []
    dept_id = applicant_user_row.get("department_id")
    for role_key in workflow_roles:
        if role_key in ("manager", "hr"):
            uid = await _get_department_roles(conn, dept_id, role_key)
            if uid:
                row = await conn.fetchrow("SELECT id, email FROM users WHERE id=$1 AND is_active=true", uid)
                if row:
                    approvers.append({"id": str(row["id"]), "email": row["email"], "role": role_key})
            # if department role not found, skip that approver silently
        else:
            # global roles like admin
            gl = await _get_global_by_role(conn, role_key)
            if gl:
                approvers.append({"id": str(gl["id"]), "email": gl["email"], "role": role_key})
    return approvers

async def apply_leave_with_upload(
    conn,
    user: dict,
    bg: BackgroundTasks,
    *,
    leave_type_id: int,
    start_date,
    end_date,
    half_day: bool,
    half_day_slot: str | None,
    reason: str | None,
    file: UploadFile | None
):
    """
    Handles leave application and optional medical document upload.
    - Sick leave: document required if > 1 day
    - Annual leave: checks balance
    """
    try:
        # --- Fetch leave type ---
        leave_type = await conn.fetchrow("SELECT id, name FROM leave_types WHERE id=$1", leave_type_id)
        if not leave_type:
            raise HTTPException(status_code=400, detail="Invalid leave type")

        lt_name = leave_type["name"].lower()
        workflow = LEAVE_WORKFLOWS.get(lt_name, ["manager", "hr"])

        # --- Fetch applicant ---
        applicant = await _get_user_with_employment(conn, user["id"])
        if not applicant:
            raise HTTPException(status_code=400, detail="Applicant not found")

        # --- Check overlapping leaves ---
        overlap = await conn.fetchrow("""
            SELECT id, start_date, end_date FROM leaves
            WHERE user_id = $1
              AND status IN ('pending', 'approved')
              AND start_date <= $3
              AND end_date >= $2
            LIMIT 1
        """, user["id"], start_date, end_date)
        if overlap:
            raise HTTPException(
                status_code=400,
                detail=f"You already have an approved or pending leave that overlaps with this period ({overlap['start_date']} to {overlap['end_date']})."
            )

        # --- Calculate requested days ---
        days_requested = _requested_leave_days(start_date, end_date, half_day, half_day_slot)
        if days_requested <= 0:
            raise HTTPException(status_code=400, detail="Invalid date range")

        # --- Check remaining leave balance ---
        lb = await _get_leave_balance(conn, user["id"], leave_type_id)
        available = float(lb["remaining"]) if lb else 0.0
        if available < days_requested:
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient {lt_name} leave balance (available: {available})."
            )

        medical_document_url = None

        # --- Sick leave validation ---
        if lt_name == "sick" and days_requested > 1:
            if not file:
                raise HTTPException(status_code=400, detail="Medical document required for sick leave more than 1 day.")

            # Upload to Supabase
            ext = (file.filename or "doc").split(".")[-1]
            path = f"medical_docs/{user['id']}/{uuid.uuid4()}.{ext}"
            content = await file.read()

            try:
                upload_res = supabase.storage.from_(SUPABASE_BUCKET).upload(path, content)
                if hasattr(upload_res, "error") and upload_res.error:
                    raise HTTPException(status_code=500, detail=f"Upload failed: {upload_res.error.message}")

                medical_document_url = supabase.storage.from_(SUPABASE_BUCKET).get_public_url(path)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"File upload failed: {str(e)}")

        # --- Insert Leave + Approvers ---
        async with conn.transaction():
            leave_row = await conn.fetchrow("""
                INSERT INTO leaves (
                    id, user_id, leave_type_id, start_date, end_date,
                    half_day, half_day_slot, reason, status, medical_document_url, created_at
                )
                VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6, $7, 'pending', $8, now())
                RETURNING id
            """, user["id"], leave_type_id, start_date, end_date,
                 half_day, half_day_slot, reason, medical_document_url)

            leave_id = str(leave_row["id"])

            # Approvers workflow
            approvers = await _resolve_approvers_for_user(conn, applicant, workflow)

            for ap in approvers:
                await conn.execute("""
                    INSERT INTO leave_approvals (id, leave_id, approver_id, approver_role, decision)
                    VALUES (gen_random_uuid(), $1, $2, $3, 'pending')
                """, leave_id, ap["id"], ap["role"])

            # Notifications
            for ap in approvers:
                payload = {"leave_id": leave_id, "by": user["email"], "role": ap["role"]}
                await create_db_notification(conn, ap["id"], "leave_requested", payload)

                if bg:
                    subject = f"Leave request from {user['email']}"
                    body = (
                        f"{user['email']} applied for {lt_name} leave from {start_date} to {end_date}.\n"
                        f"{'Medical document attached.' if medical_document_url else ''}"
                    )
                    send_email_background(bg, ap["email"], subject, body)

        return {
            "status": "success",
            "message": "Leave applied successfully",
            "leave_id": leave_id,
            "medical_document_url": medical_document_url
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Leave apply failed: {str(e)}")



async def _all_approvals_decisions(conn, leave_id: str):
    rows = await conn.fetch("SELECT decision FROM leave_approvals WHERE leave_id=$1", leave_id)
    return [r["decision"] for r in rows]


async def approve_leave(conn, user, leave_id: str, comment: Optional[str], bg: BackgroundTasks):
    """
    Approver approves leave.
    When all approvals complete → mark approved + deduct from balance.
    """
    try:
        approver = await conn.fetchrow("""
            SELECT id, decision, approver_role FROM leave_approvals
            WHERE leave_id=$1 AND approver_id=$2
            LIMIT 1
        """, leave_id, user["id"])

        if not approver:
            raise HTTPException(status_code=403, detail="You are not an approver for this leave")

        if approver["decision"] == "approved":
            return {"status": "fail", "message": "Already approved by you"}

        if approver["decision"] == "rejected":
            return {"status": "fail", "message": "This leave was already rejected by you"}

        async with conn.transaction():
            # Mark this approver approved
            await conn.execute("""
                UPDATE leave_approvals
                SET decision='approved', decided_at=now()
                WHERE id=$1
            """, approver["id"])

            # Check all approvals
            decisions = await _all_approvals_decisions(conn, leave_id)

            if "rejected" in decisions:
                await conn.execute("UPDATE leaves SET status='rejected' WHERE id=$1", leave_id)

            elif all(d == "approved" for d in decisions):
                # Mark leave approved
                await conn.execute("UPDATE leaves SET status='approved' WHERE id=$1", leave_id)

                # Deduct from balance
                lr = await conn.fetchrow("""
                    SELECT user_id, leave_type_id, start_date, end_date, half_day, half_day_slot
                    FROM leaves WHERE id=$1
                """, leave_id)
                if lr:
                    used_days = _requested_leave_days(lr["start_date"], lr["end_date"], lr["half_day"], lr["half_day_slot"])
                    await _increment_used_days(conn, str(lr["user_id"]), int(lr["leave_type_id"]), used_days)

            # Notify applicant
            applicant = await conn.fetchrow("SELECT user_id FROM leaves WHERE id=$1", leave_id)
            if applicant:
                app_id = applicant["user_id"]
                await create_db_notification(conn, str(app_id), "leave_approval_update", {"leave_id": leave_id})
                if bg:
                    app_email_row = await conn.fetchrow("SELECT email FROM users WHERE id=$1", app_id)
                    if app_email_row:
                        send_email_background(bg, app_email_row["email"],
                            "Leave Update", f"Your leave {leave_id} has been approved by {user['email']}.")

        return {"status": "success", "message": "Approved"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Approve failed: {str(e)}")



async def reject_leave(conn, user, leave_id: str, comment: Optional[str], bg: BackgroundTasks):
    """
    Approver rejects; set their decision to rejected and immediately set leave.status='rejected'.
    """
    try:
        approver = await conn.fetchrow("""
            SELECT id, decision FROM leave_approvals
            WHERE leave_id=$1 AND approver_id=$2
            LIMIT 1
        """, leave_id, user["id"])

        if not approver:
            raise HTTPException(status_code=403, detail="You are not an approver for this leave")

        if approver["decision"] == "rejected":
            return {"status": "fail", "message": "Already rejected by you"}

        async with conn.transaction():
            await conn.execute("""
                UPDATE leave_approvals
                SET decision='rejected', decided_at=now()
                WHERE id=$1
            """, approver["id"])

            # mark overall leave rejected
            await conn.execute("UPDATE leaves SET status='rejected' WHERE id=$1", leave_id)

            # notify applicant
            applicant_id_row = await conn.fetchrow("SELECT user_id FROM leaves WHERE id=$1", leave_id)
            if applicant_id_row:
                applicant_id = applicant_id_row["user_id"]
                await create_db_notification(conn, str(applicant_id), "leave_rejected", {"leave_id": leave_id, "by": user["id"], "comment": comment})
                if bg:
                    # fetch applicant email
                    app_email_row = await conn.fetchrow("SELECT email FROM users WHERE id=$1", applicant_id)
                    if app_email_row:
                        subject = "Your leave was rejected"
                        body = f"Your leave {leave_id} was rejected by {user['email']}. Comment: {comment}"
                        send_email_background(bg, app_email_row["email"], subject, body)

        return {"status": "success", "message": "Rejected"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Reject failed: {str(e)}")


async def get_my_approvals(conn, user):
    """
    Return all leaves where the logged-in user is an approver.
    """
    rows = await conn.fetch("""
        SELECT 
            l.id as leave_id,
            u.email as applicant_email,
            lt.name as leave_type,
            l.start_date, l.end_date,
            l.status as overall_status,
            la.decision as my_decision
        FROM leave_approvals la
        JOIN leaves l ON la.leave_id = l.id
        JOIN users u ON l.user_id = u.id
        JOIN leave_types lt ON l.leave_type_id = lt.id
        WHERE la.approver_id=$1
        ORDER BY l.created_at DESC
    """, user["id"])

    return [
        {
            "leave_id": str(r["leave_id"]),
            "applicant_email": r["applicant_email"],
            "leave_type": r["leave_type"],
            "start_date": r["start_date"].isoformat(),
            "end_date": r["end_date"].isoformat(),
            "status": r["overall_status"],
            "my_decision": r["my_decision"]
        }
        for r in rows
    ]


async def get_my_leaves(conn, user):
    """
    Return all leaves created by the logged-in employee,
    including approval trail.
    """
    rows = await conn.fetch("""
        SELECT 
            l.id as leave_id,
            lt.name as leave_type,
            l.start_date, l.end_date,
            l.reason,
            l.status
        FROM leaves l
        JOIN leave_types lt ON l.leave_type_id = lt.id
        WHERE l.user_id=$1
        ORDER BY l.created_at DESC
    """, user["id"])

    result = []
    for r in rows:
        approvals = await conn.fetch("""
            SELECT la.approver_role, la.decision, la.decided_at, u.email as approver_email
            FROM leave_approvals la
            JOIN users u ON la.approver_id = u.id
            WHERE la.leave_id=$1
            ORDER BY la.decided_at NULLS FIRST
        """, r["leave_id"])

        approvals_list = [
            {
                "approver_email": a["approver_email"],
                "approver_role": a["approver_role"],
                "decision": a["decision"],
                "decided_at": a["decided_at"].isoformat() if a["decided_at"] else None
            }
            for a in approvals
        ]

        result.append({
            "leave_id": str(r["leave_id"]),
            "leave_type": r["leave_type"],
            "start_date": r["start_date"].isoformat(),
            "end_date": r["end_date"].isoformat(),
            "reason": r["reason"],
            "status": r["status"],
            "approvals": approvals_list
        })
    return result



from datetime import datetime

async def get_leave_calendar(conn, month: Optional[str] = None):
    """
    Return holidays + approved leaves for a given month.
    month format: "YYYY-MM"
    """
    if not month:
        month = datetime.utcnow().strftime("%Y-%m")

    year, mon = map(int, month.split("-"))
    first_day = datetime(year, mon, 1).date()
    # calculate next month first day
    if mon == 12:
        next_month = datetime(year+1, 1, 1).date()
    else:
        next_month = datetime(year, mon+1, 1).date()

    # holidays
    holidays = await conn.fetch("""
        SELECT date, name FROM holidays
        WHERE date >= $1 AND date < $2
        ORDER BY date
    """, first_day, next_month)

    # approved leaves
    leaves = await conn.fetch("""
        SELECT u.email as employee, lt.name as leave_type, l.start_date, l.end_date
        FROM leaves l
        JOIN users u ON l.user_id = u.id
        JOIN leave_types lt ON l.leave_type_id = lt.id
        WHERE l.status='approved'
          AND l.start_date < $2
          AND l.end_date >= $1
        ORDER BY l.start_date
    """, first_day, next_month)

    return {
        "month": month,
        "holidays": [{"date": h["date"].isoformat(), "name": h["name"]} for h in holidays],
        "approved_leaves": [
            {
                "employee": r["employee"],
                "leave_type": r["leave_type"],
                "start_date": r["start_date"].isoformat(),
                "end_date": r["end_date"].isoformat()
            }
            for r in leaves
        ]
    }


async def cancel_leave(conn, user, leave_id: str, bg: BackgroundTasks):
    """
    Employee cancels their own pending leave.
    """
    try:
        # check ownership + status
        leave = await conn.fetchrow("SELECT user_id, status FROM leaves WHERE id=$1", leave_id)
        if not leave:
            raise HTTPException(status_code=404, detail="Leave not found")

        if str(leave["user_id"]) != str(user["id"]):
            raise HTTPException(status_code=403, detail="You cannot cancel this leave")

        if leave["status"] not in ("pending", "approved"):
            raise HTTPException(status_code=400, detail=f"Cannot cancel a {leave['status']} leave")

        async with conn.transaction():
            # If approved, credit back the balance
            if leave["status"] == "approved":
                lr = await conn.fetchrow("""
                    SELECT user_id, leave_type_id, start_date, end_date, half_day, half_day_slot
                    FROM leaves WHERE id=$1
                """, leave_id)
                if lr:
                    used_days = _requested_leave_days(lr["start_date"], lr["end_date"], lr["half_day"], lr["half_day_slot"])
                    await _increment_used_days(conn, str(lr["user_id"]), int(lr["leave_type_id"]), -used_days)

            # update leave + approvals
            await conn.execute("UPDATE leaves SET status='cancelled' WHERE id=$1", leave_id)
            await conn.execute("UPDATE leave_approvals SET decision='cancelled', decided_at=now() WHERE leave_id=$1", leave_id)

            # notify approvers
            approvers = await conn.fetch("""
                SELECT u.email, u.id
                FROM leave_approvals la
                JOIN users u ON la.approver_id = u.id
                WHERE la.leave_id=$1
            """, leave_id)

            for ap in approvers:
                await create_db_notification(conn, str(ap["id"]), "leave_cancelled", {"leave_id": leave_id, "by": user["email"]})
                if bg:
                    subject = f"Leave {leave_id} Cancelled"
                    body = f"{user['email']} cancelled their leave request."
                    send_email_background(bg, ap["email"], subject, body)

        return {"status": "success", "message": "Leave cancelled successfully", "leave_id": leave_id}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cancel leave failed: {str(e)}")
