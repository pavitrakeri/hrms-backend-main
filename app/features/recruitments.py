from fastapi import HTTPException, BackgroundTasks
from app.features.notifications import create_db_notification, send_email_background

async def raise_recruitment(conn, user, req: dict, bg: BackgroundTasks):
    """
    Managers and HR can raise recruitment requests.
    - If line_manager raises: approvals = HR → CFO
    - If HR raises: approvals = CFO only
    """
    # ✅ Identify caller role
    caller_role = await conn.fetchval("""
        SELECT r.name FROM roles r
        JOIN users u ON u.role_id = r.id
        WHERE u.id=$1
    """, user["id"])

    if caller_role not in ("line_manager", "hr"):
        raise HTTPException(
            status_code=403,
            detail="Only Line Managers or HR can raise recruitment requests"
        )

    # ✅ Fetch department info
    dept = await conn.fetchrow("""
        SELECT id, hr_id FROM departments WHERE name=$1
    """, req.department)

    if not dept:
        raise HTTPException(status_code=400, detail="Department not found")

    async with conn.transaction():
        # ✅ Create new recruitment request
        row = await conn.fetchrow("""
            INSERT INTO recruitments (
                id, manager_id, position, department_id, budget, job_description, status, created_at
            )
            VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, 'pending', now())
            RETURNING id
        """, user["id"], req.position, dept["id"], req.budget, req.job_description)

        recruitment_id = str(row["id"])

        # ✅ Case 1: Line Manager raises (HR → CFO approval)
        if caller_role == "line_manager":
            # --- HR approval ---
            if dept["hr_id"]:
                await conn.execute("""
                    INSERT INTO recruitment_approvals (id, recruitment_id, approver_id, approver_role)
                    VALUES (gen_random_uuid(), $1, $2, 'hr')
                """, recruitment_id, dept["hr_id"])

                await create_db_notification(
                    conn,
                    str(dept["hr_id"]),
                    "recruitment_requested",
                    {"recruitment_id": recruitment_id}
                )

            # --- CFO approval ---
            cfo = await conn.fetchrow("""
                SELECT u.id FROM users u
                JOIN roles r ON u.role_id = r.id
                WHERE r.name ILIKE 'cfo' AND u.is_active=true
                LIMIT 1
            """)
            if cfo:
                await conn.execute("""
                    INSERT INTO recruitment_approvals (id, recruitment_id, approver_id, approver_role)
                    VALUES (gen_random_uuid(), $1, $2, 'cfo')
                """, recruitment_id, cfo["id"])

                await create_db_notification(
                    conn,
                    str(cfo["id"]),
                    "recruitment_requested",
                    {"recruitment_id": recruitment_id}
                )

        # ✅ Case 2: HR raises (CFO approval only)
        elif caller_role == "hr":
            cfo = await conn.fetchrow("""
                SELECT u.id FROM users u
                JOIN roles r ON u.role_id = r.id
                WHERE r.name ILIKE 'cfo' AND u.is_active=true
                LIMIT 1
            """)
            if not cfo:
                raise HTTPException(status_code=400, detail="No active CFO found to approve this request")

            await conn.execute("""
                INSERT INTO recruitment_approvals (id, recruitment_id, approver_id, approver_role)
                VALUES (gen_random_uuid(), $1, $2, 'cfo')
            """, recruitment_id, cfo["id"])

            await create_db_notification(
                conn,
                str(cfo["id"]),
                "recruitment_requested",
                {"recruitment_id": recruitment_id}
            )

    return {
        "status": "success",
        "message": f"Recruitment request raised by {caller_role.upper()}",
        "recruitment_id": recruitment_id
    }


async def approve_recruitment(conn, user, recruitment_id: str, bg: BackgroundTasks):
    approver = await conn.fetchrow("""
        SELECT id, decision FROM recruitment_approvals
        WHERE recruitment_id=$1 AND approver_id=$2
    """, recruitment_id, user["id"])
    if not approver:
        raise HTTPException(status_code=403, detail="Not an approver")

    if approver["decision"] != "pending":
        return {"status": "fail", "message": "Already acted"}

    async with conn.transaction():
        await conn.execute("UPDATE recruitment_approvals SET decision='approved', decided_at=now() WHERE id=$1", approver["id"])
        decs = await conn.fetch("SELECT decision FROM recruitment_approvals WHERE recruitment_id=$1", recruitment_id)
        if all(d["decision"] == "approved" for d in decs):
            await conn.execute("UPDATE recruitments SET status='approved' WHERE id=$1", recruitment_id)

    return {"status": "success", "message": "Recruitment approved"}


async def reject_recruitment(conn, user, recruitment_id: str, bg: BackgroundTasks):
    approver = await conn.fetchrow("""
        SELECT id FROM recruitment_approvals
        WHERE recruitment_id=$1 AND approver_id=$2
    """, recruitment_id, user["id"])
    if not approver:
        raise HTTPException(status_code=403, detail="Not an approver")

    async with conn.transaction():
        await conn.execute("UPDATE recruitment_approvals SET decision='rejected', decided_at=now() WHERE id=$1", approver["id"])
        await conn.execute("UPDATE recruitments SET status='rejected' WHERE id=$1", recruitment_id)

    return {"status": "success", "message": "Recruitment rejected"}


async def get_my_recruitments(conn, user):
    rows = await conn.fetch("""
        SELECT r.id, r.position, d.name as department, r.budget, r.job_description, r.status
        FROM recruitments r
        JOIN departments d ON r.department_id=d.id
        WHERE r.manager_id=$1
        ORDER BY r.created_at DESC
    """, user["id"])

    result = []
    for r in rows:
        approvals = await conn.fetch("""
            SELECT ra.approver_role, ra.decision, ra.decided_at, u.email
            FROM recruitment_approvals ra
            JOIN users u ON ra.approver_id = u.id
            WHERE ra.recruitment_id=$1
        """, r["id"])
        approvals_list = [
            {"approver_email": a["email"], "approver_role": a["approver_role"], "decision": a["decision"],
             "decided_at": a["decided_at"].isoformat() if a["decided_at"] else None}
            for a in approvals
        ]
        result.append({
            "recruitment_id": str(r["id"]),
            "position": r["position"],
            "department": r["department"],
            "budget": float(r["budget"]) if r["budget"] else None,
            "job_description": r["job_description"],
            "status": r["status"],
            "approvals": approvals_list
        })
    return result


async def get_my_recruitment_approvals(conn, user):
    rows = await conn.fetch("""
        SELECT r.id as recruitment_id, r.position, d.name as department,
               r.budget, r.job_description, r.status, ra.decision as my_decision,
               u.email as manager_email
        FROM recruitment_approvals ra
        JOIN recruitments r ON ra.recruitment_id = r.id
        JOIN users u ON r.manager_id = u.id
        JOIN departments d ON r.department_id = d.id
        WHERE ra.approver_id=$1
        ORDER BY r.created_at DESC
    """, user["id"])

    return [
        {
            "recruitment_id": str(r["recruitment_id"]),
            "position": r["position"],
            "department": r["department"],
            "budget": float(r["budget"]) if r["budget"] else None,
            "job_description": r["job_description"],
            "status": r["status"],
            "my_decision": r["my_decision"],
            "manager_email": r["manager_email"]
        }
        for r in rows
    ]
