from fastapi import HTTPException, BackgroundTasks
from datetime import datetime
from app.features.notifications import create_db_notification, send_email_background

RESIGNATION_WORKFLOW = ["manager", "hr", "admin"]

async def apply_resignation(conn, user, req, bg: BackgroundTasks):
    try:
        applicant = await conn.fetchrow("SELECT id, email, department_id FROM users WHERE id=$1", user["id"])
        if not applicant:
            raise HTTPException(status_code=400, detail="Applicant not found")

        async with conn.transaction():
            row = await conn.fetchrow("""
                INSERT INTO resignations (id, user_id, reason, last_working_day, status)
                VALUES (gen_random_uuid(), $1, $2, $3, 'pending')
                RETURNING id
            """, user["id"], req.reason, req.last_working_day)
            resignation_id = str(row["id"])

            # resolve approvers: manager, hr, admin
            approvers = []
            dept = await conn.fetchrow("SELECT manager_id, hr_id FROM departments WHERE id=$1", applicant["department_id"])
            if dept and dept["manager_id"]:
                approvers.append({"id": str(dept["manager_id"]), "role": "manager"})
            if dept and dept["hr_id"]:
                approvers.append({"id": str(dept["hr_id"]), "role": "hr"})
            admin_row = await conn.fetchrow("""
                SELECT u.id, u.email FROM users u
                JOIN roles r ON u.role_id = r.id
                WHERE r.name='admin' AND u.is_active=true LIMIT 1
            """)
            if admin_row:
                approvers.append({"id": str(admin_row["id"]), "role": "admin"})

            # insert approvals
            for ap in approvers:
                await conn.execute("""
                    INSERT INTO resignation_approvals (id, resignation_id, approver_id, approver_role, decision)
                    VALUES (gen_random_uuid(), $1, $2, $3, 'pending')
                """, resignation_id, ap["id"], ap["role"])
                await create_db_notification(conn, ap["id"], "resignation_requested", {"resignation_id": resignation_id})
                if bg:
                    send_email_background(bg, user["email"], f"Resignation Request", f"New resignation to review: {resignation_id}")

        return {"status": "success", "message": "Resignation applied", "resignation_id": resignation_id}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Resignation apply failed: {str(e)}")

async def approve_resignation(conn, user, resignation_id: str, bg: BackgroundTasks):
    approver = await conn.fetchrow("""
        SELECT id, decision FROM resignation_approvals
        WHERE resignation_id=$1 AND approver_id=$2
    """, resignation_id, user["id"])
    if not approver:
        raise HTTPException(status_code=403, detail="Not an approver")

    if approver["decision"] != "pending":
        return {"status": "fail", "message": "Already acted"}

    async with conn.transaction():
        await conn.execute("UPDATE resignation_approvals SET decision='approved', decided_at=now() WHERE id=$1", approver["id"])

        # check if all approvers approved
        decs = await conn.fetch("SELECT decision FROM resignation_approvals WHERE resignation_id=$1", resignation_id)
        if all(d["decision"] == "approved" for d in decs):
            await conn.execute("UPDATE resignations SET status='approved' WHERE id=$1", resignation_id)

    return {"status": "success", "message": "Resignation approved"}

async def reject_resignation(conn, user, resignation_id: str, comment: str, bg: BackgroundTasks):
    approver = await conn.fetchrow("""
        SELECT id, decision FROM resignation_approvals
        WHERE resignation_id=$1 AND approver_id=$2
    """, resignation_id, user["id"])
    if not approver:
        raise HTTPException(status_code=403, detail="Not an approver")

    async with conn.transaction():
        await conn.execute("UPDATE resignation_approvals SET decision='rejected', decided_at=now() WHERE id=$1", approver["id"])
        await conn.execute("UPDATE resignations SET status='rejected' WHERE id=$1", resignation_id)

    return {"status": "success", "message": "Resignation rejected"}

async def cancel_resignation(conn, user, resignation_id: str, bg: BackgroundTasks):
    res = await conn.fetchrow("SELECT user_id, status FROM resignations WHERE id=$1", resignation_id)
    if not res:
        raise HTTPException(status_code=404, detail="Not found")
    if str(res["user_id"]) != str(user["id"]):
        raise HTTPException(status_code=403, detail="Not your resignation")
    if res["status"] != "pending":
        raise HTTPException(status_code=400, detail="Cannot cancel once processed")

    async with conn.transaction():
        await conn.execute("UPDATE resignations SET status='cancelled' WHERE id=$1", resignation_id)
        await conn.execute("UPDATE resignation_approvals SET decision='cancelled', decided_at=now() WHERE resignation_id=$1", resignation_id)

    return {"status": "success", "message": "Resignation cancelled", "resignation_id": resignation_id}

async def get_my_resignations(conn, user):
    rows = await conn.fetch("""
        SELECT r.id, r.reason, r.last_working_day, r.status
        FROM resignations r WHERE r.user_id=$1
        ORDER BY r.created_at DESC
    """, user["id"])

    result = []
    for r in rows:
        approvals = await conn.fetch("""
            SELECT ra.approver_role, ra.decision, ra.decided_at, u.email
            FROM resignation_approvals ra
            JOIN users u ON ra.approver_id = u.id
            WHERE ra.resignation_id=$1
        """, r["id"])
        approvals_list = [
            {"approver_email": a["email"], "approver_role": a["approver_role"], "decision": a["decision"],
             "decided_at": a["decided_at"].isoformat() if a["decided_at"] else None}
            for a in approvals
        ]
        result.append({
            "resignation_id": str(r["id"]),
            "reason": r["reason"],
            "last_working_day": r["last_working_day"].isoformat(),
            "status": r["status"],
            "approvals": approvals_list
        })
    return result


async def get_my_resignation_approvals(conn, user):
    """
    Return all resignations where the logged-in user is an approver.
    """
    rows = await conn.fetch("""
        SELECT 
            r.id as resignation_id,
            u.email as applicant_email,
            r.reason,
            r.last_working_day,
            r.status as overall_status,
            ra.decision as my_decision
        FROM resignation_approvals ra
        JOIN resignations r ON ra.resignation_id = r.id
        JOIN users u ON r.user_id = u.id
        WHERE ra.approver_id=$1
        ORDER BY r.created_at DESC
    """, user["id"])

    return [
        {
            "resignation_id": str(r["resignation_id"]),
            "applicant_email": r["applicant_email"],
            "reason": r["reason"],
            "last_working_day": r["last_working_day"].isoformat(),
            "status": r["overall_status"],
            "my_decision": r["my_decision"]
        }
        for r in rows
    ]
