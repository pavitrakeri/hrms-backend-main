# app/features/reimbursement/service.py
import os
import uuid
import json
from datetime import datetime, date
from typing import List, Optional
from fastapi import HTTPException, BackgroundTasks, UploadFile
from app.features.notifications import create_db_notification, send_email_background
from app.config import supabase, SUPABASE_BUCKET

# helper: determine workflow chain for a reimbursement
def _workflow_for_amount(amount: float):
    """
    Return list of approver role keys in order for a reimbursement amount.
    manager -> finance -> (cfo if amount >= 5000)
    We create the approvals in that order; first approver is manager.
    """
    roles = ["manager", "finance"]
    if amount >= 5000:
        roles.append("cfo")
    return roles

# helper: upload list of UploadFile to Supabase and return list of dicts {name,url,path}
async def _upload_files_to_supabase(user_id: str, reimbursement_id: str, files: List[UploadFile]):
    uploaded = []
    if not files:
        return uploaded

    # Ensure bucket exists (best-effort check)
    try:
        buckets = supabase.storage.list_buckets()
        bucket_names = [b.name for b in buckets]
        if SUPABASE_BUCKET not in bucket_names:
            raise HTTPException(status_code=500, detail=f"Supabase bucket '{SUPABASE_BUCKET}' not found")
    except HTTPException:
        raise
    except Exception:
        # ignore SDK-specific errors; attempt upload and let upload errors bubble
        pass

    for f in files:
        try:
            ext = f.filename.split(".")[-1] if "." in f.filename else ""
            filename = f"{uuid.uuid4()}.{ext}" if ext else str(uuid.uuid4())
            path = f"reimbursements/{user_id}/{reimbursement_id}/{filename}"

            data = await f.read()  # bytes

            upload_res = supabase.storage.from_(SUPABASE_BUCKET).upload(path, data)
            # SDK variations: check `.error` or returned dict
            if hasattr(upload_res, "error") and upload_res.error:
                raise HTTPException(status_code=500, detail=f"Upload failed: {upload_res.error.message}")
            if isinstance(upload_res, dict) and upload_res.get("error"):
                raise HTTPException(status_code=500, detail=f"Upload failed: {upload_res['error'].get('message') or upload_res}")

            # get public or signed URL (we will use public url for now; for private use signed URL)
            public = supabase.storage.from_(SUPABASE_BUCKET).get_public_url(path)
            # sometimes SDK returns object; normalize to string URL
            url = public if isinstance(public, str) else (public.get("publicURL") or public.get("url") or str(public))

            uploaded.append({"name": f.filename, "url": url, "path": path})
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"File upload failed: {str(e)}")

    return uploaded

# helper: resolve user ids for role in department (manager/hr/global roles)
async def _resolve_approver_for_role(conn, user_row, role_key: str):
    """
    role_key: 'manager', 'finance', 'cfo'
    For 'manager' we use user's manager_id (if exists).
    For 'finance' and 'cfo' we pick one active user with that role (first created).
    Returns user row or None.
    """
    if role_key == "manager":
        manager_id = user_row.get("manager_id")
        if not manager_id:
            return None
        row = await conn.fetchrow("SELECT id, email FROM users WHERE id=$1 AND is_active=true", manager_id)
        return row
    else:
        # global role
        row = await conn.fetchrow("""
            SELECT u.id, u.email FROM users u
            JOIN roles r ON u.role_id = r.id
            WHERE r.name ILIKE $1 AND u.is_active=true
            ORDER BY u.created_at ASC LIMIT 1
        """, role_key)
        return row

# APPLY reimbursement (handles file uploads)
async def apply_reimbursement(conn, user, category: str, subcategory: Optional[str], amount: float, description: Optional[str],
                              expense_date: date, files: Optional[List[UploadFile]], bg: BackgroundTasks):
    try:
        # Basic validations
        today = date.today()
        if expense_date > today:
            raise HTTPException(status_code=400, detail="Expense date cannot be in the future.")
        if (today - expense_date).days > 30:
            raise HTTPException(status_code=400, detail="Reimbursement must be submitted within 30 days of expense.")
        if amount <= 0:
            raise HTTPException(status_code=400, detail="Amount must be > 0.")
        if not files or len(files) == 0:
            raise HTTPException(status_code=400, detail="Supporting document(s) required.")

        # check duplicate
        dup = await conn.fetchval("""
            SELECT 1 FROM reimbursements
            WHERE user_id=$1 AND category=$2 AND COALESCE(subcategory,'')=COALESCE($3,'') AND amount=$4 AND expense_date=$5
        """, user["id"], category, subcategory, amount, expense_date)
        if dup:
            raise HTTPException(status_code=400, detail="Duplicate reimbursement request found.")

        # create a placeholder reimbursement id so files path is deterministic
        reimbursement_id = str(uuid.uuid4())

        # upload files
        uploaded_files = await _upload_files_to_supabase(user["id"], reimbursement_id, files)

        # insert reimbursement (use our generated uuid)
        row = await conn.fetchrow("""
            INSERT INTO reimbursements (id, user_id, category, subcategory, amount, description, expense_date, supporting_docs, status, created_at)
            VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8::jsonb, 'pending', now())
            RETURNING id
        """, reimbursement_id, user["id"], category, subcategory, amount, description, expense_date, json.dumps(uploaded_files))

        # create approval entries based on workflow
        roles = _workflow_for_amount(amount)  # ['manager','finance', 'cfo' maybe]
        # resolve user row for applicant (to find manager)
        applicant_row = await conn.fetchrow("SELECT id, manager_id FROM users WHERE id=$1", user["id"])

        for role_key in roles:
            approver_row = await _resolve_approver_for_role(conn, applicant_row, role_key)
            if approver_row:
                await conn.execute("""
                    INSERT INTO reimbursement_approvals (id, reimbursement_id, approver_id, approver_role)
                    VALUES (gen_random_uuid(), $1, $2, $3)
                """, reimbursement_id, approver_row["id"], role_key)
                # notify approver
                await create_db_notification(conn, str(approver_row["id"]), "reimbursement_requested", {"reimbursement_id": reimbursement_id})
                if bg:
                    # send email to approver
                    subject = f"Reimbursement request from {user['email']}"
                    body = f"User {user['email']} submitted a reimbursement of {amount} for {category}. Reimbursement ID: {reimbursement_id}"
                    send_email_background(bg, approver_row["email"], subject, body)
            else:
                # if an approver for that role can't be resolved we continue; omission means that step is skipped
                continue

        return {"status": "success", "message": "Reimbursement submitted successfully", "reimbursement_id": reimbursement_id}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Apply failed: {str(e)}")

# EDIT reimbursement (only when pending and before any approver acted)
async def edit_reimbursement(conn, user, reimbursement_id: str, category: str, subcategory: Optional[str], amount: float,
                             description: Optional[str], expense_date: date, files: Optional[List[UploadFile]]):
    try:
        rec = await conn.fetchrow("SELECT id, status FROM reimbursements WHERE id=$1 AND user_id=$2", reimbursement_id, user["id"])
        if not rec:
            raise HTTPException(status_code=404, detail="Reimbursement not found")
        if rec["status"] not in ("pending", "queried"):
            raise HTTPException(status_code=400, detail="Only reimbursements in 'pending' or 'queried' state can be edited")


        # upload new files if supplied
        uploaded_files = None
        if files and len(files) > 0:
            uploaded_files = await _upload_files_to_supabase(user["id"], reimbursement_id, files)

        # update fields (if files provided, replace supporting_docs)
        if uploaded_files:
            await conn.execute("""
                UPDATE reimbursements
                SET category=$1, subcategory=$2, amount=$3, description=$4, expense_date=$5, supporting_docs=$6::jsonb, updated_at=now()
                WHERE id=$7
            """, category, subcategory, amount, description, expense_date, json.dumps(uploaded_files), reimbursement_id)
        else:
            await conn.execute("""
                UPDATE reimbursements
                SET category=$1, subcategory=$2, amount=$3, description=$4, expense_date=$5, updated_at=now()
                WHERE id=$6
            """, category, subcategory, amount, description, expense_date, reimbursement_id)

        return {"status": "success", "message": "Reimbursement updated successfully"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Edit failed: {str(e)}")

# LIST my reimbursements
async def list_my_reimbursements(conn, user):
    try:
        rows = await conn.fetch("""
            SELECT r.id, r.category, r.subcategory, r.amount, r.expense_date, r.status,
                   (SELECT ra.approver_role FROM reimbursement_approvals ra
                    WHERE ra.reimbursement_id=r.id AND ra.decision='pending' LIMIT 1) as pending_with,
                   r.supporting_docs
            FROM reimbursements r
            WHERE r.user_id=$1
            ORDER BY r.created_at DESC
        """, user["id"])

        result = []
        for r in rows:
            result.append({
                "reimbursement_id": str(r["id"]),
                "category": r["category"],
                "subcategory": r["subcategory"],
                "amount": float(r["amount"]),
                "expense_date": r["expense_date"],
                "status": r["status"],
                "pending_with": r["pending_with"],
                "supporting_docs": r["supporting_docs"]
            })
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"List failed: {str(e)}")

# GET reimbursement detail (including approval history)
async def get_reimbursement_detail(conn, user, reimbursement_id: str):
    try:
        row = await conn.fetchrow("""
            SELECT r.id, r.user_id, r.category, r.subcategory, r.amount, r.description, r.expense_date,
                   r.supporting_docs, r.status, r.created_at, r.decided_at
            FROM reimbursements r
            WHERE r.id=$1
        """, reimbursement_id)
        if not row:
            raise HTTPException(status_code=404, detail="Reimbursement not found")

        # approvals history
        approvals = await conn.fetch("""
            SELECT ra.approver_id, ra.approver_role, ra.decision, ra.comment, ra.decided_at, u.email as approver_email
            FROM reimbursement_approvals ra
            LEFT JOIN users u ON ra.approver_id = u.id
            WHERE ra.reimbursement_id=$1
            ORDER BY ra.decided_at NULLS FIRST
        """, reimbursement_id)

        approvals_list = []
        for a in approvals:
            approvals_list.append({
                "approver_id": str(a["approver_id"]) if a["approver_id"] else None,
                "approver_role": a["approver_role"],
                "approver_email": a["approver_email"],
                "decision": a["decision"],
                "comment": a["comment"],
                "decided_at": a["decided_at"].isoformat() if a["decided_at"] else None
            })

        return {
            "reimbursement_id": str(row["id"]),
            "user_id": str(row["user_id"]),
            "category": row["category"],
            "subcategory": row["subcategory"],
            "amount": float(row["amount"]),
            "description": row["description"],
            "expense_date": row["expense_date"].isoformat(),
            "supporting_docs": row["supporting_docs"] or [],
            "status": row["status"],
            "approvals": approvals_list,
            "created_at": row["created_at"].isoformat(),
            "decided_at": row["decided_at"].isoformat() if row["decided_at"] else None
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Detail failed: {str(e)}")

# Approve reimbursement (by an approver)
async def approve_reimbursement(conn, user, reimbursement_id: str, comment: Optional[str], bg: BackgroundTasks):
    """
    Approver approves the reimbursement. Flow:
    - Find the pending approval row for this approver.
    - Set decision='approved', decided_at=now()
    - If this was the last pending approver -> mark reimbursements.status='approved' and notify employee
    - Else leave next approver pending and notify them
    """
    try:
        approver_row = await conn.fetchrow("""
            SELECT id, decision, approver_role FROM reimbursement_approvals
            WHERE reimbursement_id=$1 AND approver_id=$2
            LIMIT 1
        """, reimbursement_id, user["id"])
        if not approver_row:
            raise HTTPException(status_code=403, detail="You are not an approver for this reimbursement")

        if approver_row["decision"] != "pending":
            return {"status": "fail", "message": "Already acted"}

        async with conn.transaction():
            # mark this approver approved
            await conn.execute("""
                UPDATE reimbursement_approvals SET decision='approved', comment=$1, decided_at=now()
                WHERE id=$2
            """, comment, approver_row["id"])

            # check if any pending approvers remain
            pend = await conn.fetchval("""
                SELECT COUNT(1) FROM reimbursement_approvals
                WHERE reimbursement_id=$1 AND decision='pending'
            """, reimbursement_id)

            if pend == 0:
                # all approvals done: set reimbursement as approved
                await conn.execute("UPDATE reimbursements SET status='approved', decided_at=now() WHERE id=$1", reimbursement_id)
                # notify employee
                rec_user = await conn.fetchrow("SELECT user_id FROM reimbursements WHERE id=$1", reimbursement_id)
                if rec_user:
                    await create_db_notification(conn, str(rec_user["user_id"]), "reimbursement_approved", {"reimbursement_id": reimbursement_id})
                    if bg:
                        email_row = await conn.fetchrow("SELECT email FROM users WHERE id=$1", rec_user["user_id"])
                        if email_row:
                            send_email_background(bg, email_row["email"], "Reimbursement approved", f"Your reimbursement {reimbursement_id} has been fully approved.")
            else:
                # find the next pending approver (in order of creation)
                next_approver = await conn.fetchrow("""
                    SELECT approver_id, approver_role FROM reimbursement_approvals
                    WHERE reimbursement_id=$1 AND decision='pending'
                    ORDER BY decided_at NULLS FIRST, id ASC
                    LIMIT 1
                """, reimbursement_id)
                if next_approver:
                    await create_db_notification(conn, str(next_approver["approver_id"]), "reimbursement_requested", {"reimbursement_id": reimbursement_id})
                    if bg:
                        email_row = await conn.fetchrow("SELECT email FROM users WHERE id=$1", next_approver["approver_id"])
                        if email_row:
                            send_email_background(bg, email_row["email"], "Reimbursement awaiting your approval", f"Reimbursement {reimbursement_id} requires your approval.")

        return {"status": "success", "message": "Approved"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Approve failed: {str(e)}")

# Reject reimbursement
async def reject_reimbursement(conn, user, reimbursement_id: str, comment: Optional[str], bg: BackgroundTasks):
    try:
        approver_row = await conn.fetchrow("""
            SELECT id FROM reimbursement_approvals
            WHERE reimbursement_id=$1 AND approver_id=$2
            LIMIT 1
        """, reimbursement_id, user["id"])
        if not approver_row:
            raise HTTPException(status_code=403, detail="You are not an approver for this reimbursement")

        async with conn.transaction():
            await conn.execute("""
                UPDATE reimbursement_approvals SET decision='rejected', comment=$1, decided_at=now()
                WHERE id=$2
            """, comment, approver_row["id"])

            # mark reimbursement rejected
            await conn.execute("UPDATE reimbursements SET status='rejected', decided_at=now() WHERE id=$1", reimbursement_id)

            # notify employee
            rec_user = await conn.fetchrow("SELECT user_id FROM reimbursements WHERE id=$1", reimbursement_id)
            if rec_user:
                await create_db_notification(conn, str(rec_user["user_id"]), "reimbursement_rejected", {"reimbursement_id": reimbursement_id, "by": user["id"], "comment": comment})
                if bg:
                    email_row = await conn.fetchrow("SELECT email FROM users WHERE id=$1", rec_user["user_id"])
                    if email_row:
                        send_email_background(bg, email_row["email"], "Reimbursement rejected", f"Your reimbursement {reimbursement_id} was rejected. Reason: {comment}")

        return {"status": "success", "message": "Rejected"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Reject failed: {str(e)}")


# Raise a query (request for more info)
async def query_reimbursement(conn, user, reimbursement_id: str, comment: str, bg: BackgroundTasks):
    try:
        approver_row = await conn.fetchrow("""
            SELECT id, decision FROM reimbursement_approvals
            WHERE reimbursement_id=$1 AND approver_id=$2
            LIMIT 1
        """, reimbursement_id, user["id"])

        if not approver_row:
            raise HTTPException(status_code=403, detail="You are not an approver for this reimbursement")

        if approver_row["decision"] != "pending":
            raise HTTPException(status_code=400, detail="You already acted on this reimbursement")

        async with conn.transaction():
            # mark this approval as query
            await conn.execute("""
                UPDATE reimbursement_approvals 
                SET decision='query', comment=$1, decided_at=now()
                WHERE id=$2
            """, comment, approver_row["id"])

            # mark reimbursement as queried
            await conn.execute("""
                UPDATE reimbursements 
                SET status='queried', decided_at=now()
                WHERE id=$1
            """, reimbursement_id)

            # notify employee
            rec_user = await conn.fetchrow("SELECT user_id FROM reimbursements WHERE id=$1", reimbursement_id)
            if rec_user:
                await create_db_notification(
                    conn, str(rec_user["user_id"]),
                    "reimbursement_query",
                    {"reimbursement_id": reimbursement_id, "by": user["id"], "comment": comment}
                )
                if bg:
                    email_row = await conn.fetchrow("SELECT email FROM users WHERE id=$1", rec_user["user_id"])
                    if email_row:
                        send_email_background(
                            bg,
                            email_row["email"],
                            "Reimbursement queried",
                            f"Your reimbursement {reimbursement_id} requires clarification. Comment: {comment}"
                        )

        return {"status": "success", "message": "Query raised successfully"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Query failed: {str(e)}")
