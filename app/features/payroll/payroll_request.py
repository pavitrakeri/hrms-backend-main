import uuid
from fastapi import HTTPException, BackgroundTasks, UploadFile
from app.config import supabase, SUPABASE_BUCKET
from app.features.notifications import create_db_notification, send_email_background


async def apply_payroll_request(conn, user, req, bg: BackgroundTasks):
    """
    Handles submission of payroll-related requests by employees:
    - Salary Advance
    - Salary Certificate
    - Payroll Query / Dispute
    - Payment Schedule Change
    """
    user_id = user["id"]

    # --- Validate request type ---
    valid_types = ["advance", "certificate", "query", "schedule_change"]
    if req.request_type not in valid_types:
        raise HTTPException(status_code=400, detail="Invalid request type")

    # --- Salary Advance Validation ---
    if req.request_type == "advance":
        # get employee basic salary
        basic_salary = await conn.fetchval("SELECT basic_salary FROM users WHERE id=$1", user_id)
        if not basic_salary or basic_salary <= 0:
            raise HTTPException(status_code=400, detail="Invalid salary details")

        max_advance = basic_salary * 0.5
        if req.amount is None or req.amount > max_advance:
            raise HTTPException(status_code=400, detail=f"Advance cannot exceed 50% of basic salary (Max: {max_advance})")

        # check advance count this year
        count = await conn.fetchval("""
            SELECT COUNT(*) FROM payroll_requests
            WHERE employee_id=$1 AND request_type='advance' AND EXTRACT(YEAR FROM created_at)=EXTRACT(YEAR FROM now())
        """, user_id)
        if count >= 2:
            raise HTTPException(status_code=400, detail="Maximum 2 advances allowed per year")

    # --- Upload Attachments (if any) ---
    uploaded_urls = []
    if req.attachments:
        for file in req.attachments:
            if isinstance(file, UploadFile):
                ext = file.filename.split(".")[-1]
                path = f"payroll_docs/{user_id}/{uuid.uuid4()}.{ext}"
                data = await file.read()
                upload_res = supabase.storage.from_(SUPABASE_BUCKET).upload(path, data)
                if hasattr(upload_res, "error") and upload_res.error:
                    raise HTTPException(status_code=500, detail=f"Upload failed: {upload_res.error.message}")
                file_url = supabase.storage.from_(SUPABASE_BUCKET).get_public_url(path)
                uploaded_urls.append(file_url)

    # --- Insert request ---
    row = await conn.fetchrow("""
        INSERT INTO payroll_requests (
            id, employee_id, request_type, amount, purpose, query_type, reason,
            description, requested_date, attachments, status, current_approver_role
        )
        VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6, $7, $8, $9, 'pending', 'manager')
        RETURNING id
    """, user_id, req.request_type, req.amount, req.purpose, req.query_type,
         req.reason, req.description, req.requested_date, uploaded_urls or [])

    request_id = str(row["id"])

    # --- Create approval chain ---
    await _create_approval_flow(conn, user_id, req.request_type, request_id)

    return {"status": "success", "message": "Payroll request submitted", "request_id": request_id}


async def _create_approval_flow(conn, user_id, request_type, request_id):
    """
    Defines approval hierarchy based on request type
    """
    # Get manager, HR, finance, and CFO IDs
    manager_id = await conn.fetchval("SELECT manager_id FROM users WHERE id=$1", user_id)
    hr_id = await conn.fetchval("""
        SELECT u.id FROM users u
        JOIN roles r ON u.role_id = r.id
        WHERE r.name='hr' LIMIT 1
    """)
    finance_id = await conn.fetchval("""
        SELECT u.id FROM users u
        JOIN roles r ON u.role_id = r.id
        WHERE r.name='finance' LIMIT 1
    """)
    cfo_id = await conn.fetchval("""
        SELECT u.id FROM users u
        JOIN roles r ON u.role_id = r.id
        WHERE r.name='cfo' LIMIT 1
    """)

    approvers = []

    if request_type == "advance":
        approvers = [
            (manager_id, "manager"),
            (hr_id, "hr"),
            (finance_id, "finance"),
            (cfo_id, "cfo")
        ]
    elif request_type == "certificate":
        approvers = [(hr_id, "hr")]
    elif request_type == "query":
        approvers = [(finance_id, "finance"), (hr_id, "hr")]
    elif request_type == "schedule_change":
        approvers = [(hr_id, "hr"), (finance_id, "finance"), (cfo_id, "cfo")]

    for ap_id, ap_role in approvers:
        if ap_id:
            await conn.execute("""
                INSERT INTO payroll_approvals (id, payroll_request_id, approver_id, approver_role)
                VALUES (gen_random_uuid(), $1, $2, $3)
            """, request_id, ap_id, ap_role)
