from fastapi import HTTPException
from datetime import date, datetime
from typing import Optional
from app.features.payroll.payroll_calculator import payroll_preview

async def get_payslip_list(conn, user, month: Optional[date] = None, last_n_months: Optional[int] = None, from_month: Optional[date] = None, to_month: Optional[date] = None):
    """
    Returns list of payslips for the logged-in employee.
    - If month is provided, filters to that payroll cycle.
    - If last_n_months is specified (e.g. 3), returns last n months of payslips.
    - If from_month/to_month specified, returns all payslips in that range (inclusive).
    """
    base_query = """
        SELECT pi.id AS payroll_item_id, pc.month, pi.basic, pi.gross_pay,
               pi.total_deductions, pi.net_pay, pi.payslip_url, pi.status
        FROM payroll_items pi
        JOIN payroll_cycles pc ON pi.payroll_cycle_id = pc.id
        WHERE pi.user_id = $1
    """
    params = [user["id"]]

    if month:
        base_query += " AND pc.month = $2"
        params.append(month)
    elif last_n_months:
        base_query += f" AND pc.month >= (SELECT MIN(month) FROM (SELECT month FROM payroll_cycles ORDER BY month DESC LIMIT {last_n_months}) AS subq)"
    elif from_month and to_month:
        base_query += " AND pc.month BETWEEN $2 AND $3"
        params.extend([from_month, to_month])

    base_query += " ORDER BY pc.month DESC"

    rows = await conn.fetch(base_query, *params)

    return [
        {
            "payslip_id": str(r["payroll_item_id"]),
            "month": r["month"],
            "title": f"Payslip - {r['month']}",
            "description": f"Payslip for {r['month']}",
            "file_url": r["payslip_url"],
            "basic": float(r["basic"] or 0),
            "gross_pay": float(r["gross_pay"] or 0),
            "total_deductions": float(r["total_deductions"] or 0),
            "net_pay": float(r["net_pay"] or 0),
            "status": r["status"]
        }
        for r in rows
    ]

async def get_payslip_detail(conn, user, payroll_item_id: str):
    """
    Fetch full payslip detail for a specific payroll item, including file URL.
    """
    row = await conn.fetchrow("""
        SELECT pi.id AS payroll_item_id, pc.month, pi.basic, pi.gross_pay,
               pi.total_deductions, pi.net_pay, pi.allowances, pi.deductions,
               pi.payslip_url
        FROM payroll_items pi
        JOIN payroll_cycles pc ON pi.payroll_cycle_id = pc.id
        WHERE pi.user_id = $1 AND pi.id = $2
    """, user["id"], payroll_item_id)

    if not row:
        raise HTTPException(status_code=404, detail="Payslip not found")

    return {
        "status": "success",
        "payslip_id": str(row["payroll_item_id"]),
        "month": row["month"],
        "title": f"Payslip - {row['month']}",
        "description": f"Payslip for {row['month']}",
        "file_url": row["payslip_url"],
        "basic": float(row["basic"] or 0),
        "gross_pay": float(row["gross_pay"] or 0),
        "total_deductions": float(row["total_deductions"] or 0),
        "net_pay": float(row["net_pay"] or 0),
        "allowances": row["allowances"],
        "deductions": row["deductions"]
    }

async def get_current_month_payslip(conn, user):
    """
    Returns the payslip for the current month for the logged-in employee. If not present, calculates provisional (partial month) pay as of today.
    """
    today = date.today()
    # Find payroll cycle id for current month
    row = await conn.fetchrow("""
        SELECT pi.id AS payroll_item_id, pc.id as payroll_cycle_id, pc.month, pi.basic, pi.gross_pay,
               pi.total_deductions, pi.net_pay, pi.allowances, pi.deductions, pi.payslip_url, pi.status
        FROM payroll_items pi
        JOIN payroll_cycles pc ON pi.payroll_cycle_id = pc.id
        WHERE pi.user_id = $1 AND EXTRACT(YEAR FROM pc.month) = $2 AND EXTRACT(MONTH FROM pc.month) = $3
    """, user["id"], today.year, today.month)

    if row:
        return {
            "status": "success",
            "payslip_id": str(row["payroll_item_id"]),
            "month": row["month"],
            "title": f"Payslip - {row['month']}",
            "description": f"Payslip for {row['month']}",
            "file_url": row["payslip_url"],
            "basic": float(row["basic"] or 0),
            "gross_pay": float(row["gross_pay"] or 0),
            "total_deductions": float(row["total_deductions"] or 0),
            "net_pay": float(row["net_pay"] or 0),
            "allowances": row["allowances"],
            "deductions": row["deductions"],
            "provisional": False
        }
    else:
        # Use payroll_preview to generate a provisional payslip for up-to-today
        preview = await payroll_preview(conn, user["id"], date(today.year, today.month, 1), up_to_date=today)
        # Enrich with payslip-style fields
        preview.update({
            "status": "provisional",
            "payslip_id": None,
            "month": date(today.year, today.month, 1),
            "title": f"Provisional Payslip - {today.strftime('%Y-%m-%d')}",
            "description": f"Estimated payslip as of {today.strftime('%Y-%m-%d')}",
            "file_url": None,
            "provisional": True
        })
        return preview
