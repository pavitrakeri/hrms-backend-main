from fastapi import HTTPException
from typing import Dict, Any

async def get_dashboard_summary(conn, user: Dict[str, Any]):
    """
    Fetch unified dashboard summary metrics tailored to the user's role.
    """
    # 1. Fetch user details
    user_row = await conn.fetchrow("""
        SELECT u.full_name, u.email, r.name as role, COALESCE(d.name, 'Unassigned') as department
        FROM users u
        JOIN roles r ON u.role_id = r.id
        LEFT JOIN departments d ON u.department_id = d.id
        WHERE u.id = $1
    """, user["id"])
    
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")
        
    user_summary = {
        "full_name": user_row["full_name"],
        "email": user_row["email"],
        "role": user_row["role"],
        "department": user_row["department"]
    }
    
    # 2. Today's clock-in status
    today_row = await conn.fetchrow("""
        SELECT clock_in_at, clock_out_at
        FROM attendance
        WHERE user_id = $1
          AND (clock_in_at AT TIME ZONE 'UTC' + interval '5 hours 30 minutes')::date = CURRENT_DATE
        ORDER BY clock_in_at DESC
        LIMIT 1
    """, user["id"])
    
    if today_row:
        clock_in = today_row["clock_in_at"].isoformat() if today_row["clock_in_at"] else None
        clock_out = today_row["clock_out_at"].isoformat() if today_row["clock_out_at"] else None
        status = "clocked_in" if clock_out is None else "clocked_out"
    else:
        clock_in = None
        clock_out = None
        status = "not_clocked_in"
        
    attendance_today = {
        "clock_in_at": clock_in,
        "clock_out_at": clock_out,
        "status": status
    }
    
    # 3. Personal Stats Summary
    tasks_count = await conn.fetchval("""
        SELECT COUNT(*) FROM tasks
        WHERE assignee_id = $1 AND status != 'done'
    """, user["id"])
    
    projects_count = await conn.fetchval("""
        SELECT COUNT(*) FROM project_members
        WHERE user_id = $1
    """, user["id"])
    
    annual_leave_remaining = await conn.fetchval("""
        SELECT remaining FROM leave_balance
        WHERE user_id = $1 AND leave_type_id = 2 AND year = EXTRACT(YEAR FROM now())
    """, user["id"])
    
    sick_leave_remaining = await conn.fetchval("""
        SELECT remaining FROM leave_balance
        WHERE user_id = $1 AND leave_type_id = 1 AND year = EXTRACT(YEAR FROM now())
    """, user["id"])
    
    my_stats = {
        "tasks_count": tasks_count or 0,
        "projects_count": projects_count or 0,
        "annual_leave_remaining": float(annual_leave_remaining) if annual_leave_remaining is not None else 0.0,
        "sick_leave_remaining": float(sick_leave_remaining) if sick_leave_remaining is not None else 0.0
    }
    
    # 4. Upcoming holidays (next 3)
    holiday_rows = await conn.fetch("""
        SELECT name, date FROM holidays
        WHERE date >= CURRENT_DATE
        ORDER BY date ASC
        LIMIT 3
    """)
    upcoming_holidays = [
        {"name": h["name"], "date": h["date"].isoformat()}
        for h in holiday_rows
    ]
    
    # 5. Role-based Org Stats and Pending Actions
    org_stats = None
    pending_actions = []
    
    role = user_row["role"].lower().strip()
    if role in ("admin", "hr", "manager", "line_manager", "cfo"):
        total_employees = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_active = true")
        clocked_in_today = await conn.fetchval("""
            SELECT COUNT(DISTINCT user_id) FROM attendance
            WHERE (clock_in_at AT TIME ZONE 'UTC' + interval '5 hours 30 minutes')::date = CURRENT_DATE
        """)
        pending_leaves = await conn.fetchval("SELECT COUNT(*) FROM leaves WHERE status = 'pending'")
        pending_reimbursements = await conn.fetchval("SELECT COUNT(*) FROM reimbursements WHERE status = 'pending'")
        pending_resignations = await conn.fetchval("SELECT COUNT(*) FROM resignations WHERE status = 'pending'")
        pending_recruitments = await conn.fetchval("SELECT COUNT(*) FROM recruitments WHERE status = 'pending'")
        
        org_stats = {
            "total_employees": total_employees or 0,
            "clocked_in_today": clocked_in_today or 0,
            "pending_leaves": pending_leaves or 0,
            "pending_reimbursements": pending_reimbursements or 0,
            "pending_resignations": pending_resignations or 0,
            "pending_recruitments": pending_recruitments or 0
        }
        
        # Fetch pending actions requiring this user's approval
        # Leave approvals pending
        leave_approvals = await conn.fetch("""
            SELECT 
                l.id as leave_id,
                u.full_name as employee_name,
                lt.name as leave_type,
                l.start_date, l.end_date,
                l.reason
            FROM leave_approvals la
            JOIN leaves l ON la.leave_id = l.id
            JOIN users u ON l.user_id = u.id
            JOIN leave_types lt ON l.leave_type_id = lt.id
            WHERE la.approver_id = $1 AND la.decision = 'pending' AND l.status = 'pending'
            ORDER BY l.created_at DESC
        """, user["id"])
        
        for r in leave_approvals:
            date_info = f"{r['start_date'].isoformat()} to {r['end_date'].isoformat()}"
            pending_actions.append({
                "type": "leave",
                "id": str(r["leave_id"]),
                "employee_name": r["employee_name"],
                "details": f"Applied for {r['leave_type']} leave: \"{r['reason'] or 'No reason provided'}\"",
                "date_info": date_info
            })
            
        # Reimbursement approvals pending
        reim_approvals = await conn.fetch("""
            SELECT 
                r.id as reim_id,
                u.full_name as employee_name,
                r.category,
                r.subcategory,
                r.amount,
                r.description,
                r.expense_date
            FROM reimbursement_approvals ra
            JOIN reimbursements r ON ra.reimbursement_id = r.id
            JOIN users u ON r.user_id = u.id
            WHERE ra.approver_id = $1 AND ra.decision = 'pending' AND r.status = 'pending'
            ORDER BY r.created_at DESC
        """, user["id"])
        
        for r in reim_approvals:
            sub = f" ({r['subcategory']})" if r["subcategory"] else ""
            pending_actions.append({
                "type": "reimbursement",
                "id": str(r["reim_id"]),
                "employee_name": r["employee_name"],
                "details": f"Filed expense for {r['category']}{sub}: \"{r['description'] or 'No description'}\"",
                "amount": float(r["amount"]),
                "date_info": r["expense_date"].isoformat()
            })

    return {
        "user": user_summary,
        "attendance_today": attendance_today,
        "my_stats": my_stats,
        "upcoming_holidays": upcoming_holidays,
        "org_stats": org_stats,
        "pending_actions": pending_actions
    }
