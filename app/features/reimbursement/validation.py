from fastapi import HTTPException
from datetime import date, timedelta

async def validate_reimbursement(conn, user, req):
    today = date.today()

    # Expense date cannot be in future
    if req.expense_date > today:
        raise HTTPException(status_code=400, detail="Expense date cannot be in the future.")

    # Must be within 30 days of expense
    if (today - req.expense_date).days > 30:
        raise HTTPException(status_code=400, detail="Reimbursement must be submitted within 30 days of expense.")

    # Receipt is mandatory
    if not req.supporting_docs or len(req.supporting_docs) == 0:
        raise HTTPException(status_code=400, detail="Supporting document(s) required.")

    # Check duplicate (same category + date + amount)
    duplicate = await conn.fetchval("""
        SELECT 1 FROM reimbursements
        WHERE user_id=$1 AND category=$2 AND amount=$3 AND expense_date=$4
    """, user["id"], req.category, req.amount, req.expense_date)

    if duplicate:
        raise HTTPException(status_code=400, detail="Duplicate reimbursement request found.")
