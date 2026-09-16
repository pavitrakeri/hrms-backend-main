import json
import logging
from datetime import datetime, date
from typing import Dict, Any
from app.ai.guardrails import sanitize_tool_output
from app.ai.rag import search_policies
from app.features.leaves_balance import get_leave_balance

logger = logging.getLogger(__name__)

async def dispatch_tool_call(conn, tool_name: str, args: Dict[str, Any], user: Dict[str, Any]) -> Dict[str, Any]:
    """
    Safely executes an Aimploy tool call on behalf of the authenticated user.
    """
    logger.info(f"Executing tool: {tool_name} with args {args} for user {user.get('email')}")

    try:
        if tool_name == "get_my_leave_balance":
            balance_res = await get_leave_balance(conn, user, None)
            return sanitize_tool_output(balance_res, user)

        elif tool_name == "get_my_attendance_today":
            row = await conn.fetchrow("""
                SELECT clock_in_at, clock_out_at, total_seconds, location
                FROM attendance
                WHERE user_id=$1 AND date_trunc('day', clock_in_at) = date_trunc('day', now())
                ORDER BY clock_in_at DESC LIMIT 1
            """, user["id"])
            if not row:
                return {"status": "not_clocked_in", "message": "You have not clocked in today yet."}
            return {
                "status": "clocked_out" if row["clock_out_at"] else "clocked_in",
                "clock_in_at": row["clock_in_at"].isoformat() if row["clock_in_at"] else None,
                "clock_out_at": row["clock_out_at"].isoformat() if row["clock_out_at"] else None,
                "total_seconds": row["total_seconds"],
                "location": row["location"] or "Unknown Location"
            }

        elif tool_name == "get_my_tasks":
            status = args.get("status")
            if status:
                rows = await conn.fetch("""
                    SELECT t.id, t.title, t.status, t.due_date, p.name as project_name
                    FROM tasks t
                    JOIN projects p ON t.project_id = p.id
                    WHERE t.assignee_id=$1 AND t.status=$2
                    ORDER BY t.due_date ASC NULLS LAST
                """, user["id"], status)
            else:
                rows = await conn.fetch("""
                    SELECT t.id, t.title, t.status, t.due_date, p.name as project_name
                    FROM tasks t
                    JOIN projects p ON t.project_id = p.id
                    WHERE t.assignee_id=$1
                    ORDER BY t.due_date ASC NULLS LAST
                """, user["id"])

            tasks = [
                {
                    "id": str(r["id"]),
                    "title": r["title"],
                    "status": r["status"],
                    "due_date": r["due_date"].isoformat() if r["due_date"] else None,
                    "project_name": r["project_name"]
                }
                for r in rows
            ]
            return {"tasks": tasks, "count": len(tasks)}

        elif tool_name == "get_upcoming_holidays":
            rows = await conn.fetch("""
                SELECT date, name
                FROM holidays
                WHERE date >= CURRENT_DATE
                ORDER BY date ASC
                LIMIT 10
            """)
            holidays = [
                {"date": r["date"].isoformat() if r["date"] else None, "name": r["name"]}
                for r in rows
            ]
            return {"holidays": holidays}

        elif tool_name == "search_company_policies":
            query = args.get("query", "")
            results = await search_policies(conn, query, top_k=3)
            return {"results": results, "query": query}

        elif tool_name == "propose_leave_application":
            leave_type = str(args.get("leave_type", "annual")).lower().strip()
            start_date_str = args.get("start_date")
            end_date_str = args.get("end_date")
            reason = args.get("reason", "Personal reasons")
            half_day = bool(args.get("half_day", False))

            try:
                s_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
                e_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
            except Exception:
                return {"error": "Invalid date format. Dates must be in YYYY-MM-DD format."}

            if e_date < s_date:
                return {"error": "End date cannot be earlier than start date."}

            num_days = 0.5 if half_day else (e_date - s_date).days + 1

            # Get leave_type_id
            type_row = await conn.fetchrow("SELECT id, name FROM leave_types WHERE name=$1", leave_type)
            if not type_row:
                type_row = await conn.fetchrow("SELECT id, name FROM leave_types WHERE name='annual'")
            leave_type_id = type_row["id"] if type_row else 2
            leave_type_name = type_row["name"] if type_row else "annual"

            # Create an audit log record for confirmation
            action_row = await conn.fetchrow("""
                INSERT INTO ai_audit_logs (id, user_id, prompt, tool_name, tool_args, user_confirmed, status)
                VALUES (gen_random_uuid(), $1, $2, 'apply_leave', $3, false, 'pending')
                RETURNING id
            """, user["id"], f"Apply for {leave_type_name} leave from {start_date_str} to {end_date_str}", json.dumps({
                "leave_type_id": leave_type_id,
                "leave_type_name": leave_type_name,
                "start_date": start_date_str,
                "end_date": end_date_str,
                "half_day": half_day,
                "reason": reason,
                "num_days": num_days
            }))

            action_id = str(action_row["id"])

            return {
                "action_required": "confirmation",
                "action_id": action_id,
                "leave_type": leave_type_name,
                "start_date": start_date_str,
                "end_date": end_date_str,
                "num_days": num_days,
                "reason": reason,
                "message": f"I have prepared your leave request for {num_days} day(s) from {start_date_str} to {end_date_str}. Please review the confirmation card below and click Confirm to submit it."
            }

        else:
            return {"error": f"Unknown tool: {tool_name}"}

    except Exception as e:
        logger.error(f"Error in tool dispatch {tool_name}: {e}", exc_info=True)
        return {"error": f"Failed to execute tool {tool_name}: {str(e)}"}
