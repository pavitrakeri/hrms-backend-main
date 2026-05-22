from datetime import date, timedelta, datetime
from typing import Optional, Dict, Any

# --- MAIN API ---
async def calculate_employee_payroll(conn, user_id, payroll_month: date, up_to_date: Optional[date] = None) -> dict:
    """
    Calculates all payroll values for an employee in a given payroll month.
    If up_to_date is given (e.g. 10th of month), only include salary/attendance up to that day.
    Returns gross, net, detailed deductions, allowances, EOSG accrual, breakdowns.
    Does NOT write to database.
    """
    # 1. Fetch employee salary and fixed allowances.
    user_row = await conn.fetchrow("""
        SELECT id, basic_salary, joining_date, employment_status, hra, transportation, other, mobile
        FROM users
        WHERE id = $1
    """, user_id)
    if not user_row:
        return {"error": "User not found"}

    # Prefer payroll setup table if configured
    setup_row = await conn.fetchrow("""
        SELECT basic_salary, hra, allowances, other_benefits
        FROM employee_payroll_setup
        WHERE employee_id = $1
    """, user_id)

    if setup_row:
        basic_salary = float(setup_row["basic_salary"] or user_row["basic_salary"] or 0)
        allowances = {
            "hra": float(setup_row["hra"] or 0),
            "fixed_allowances": float(setup_row["allowances"] or 0),
            "other_benefits": float(setup_row["other_benefits"] or 0)
        }
    else:
        basic_salary = float(user_row["basic_salary"] or 0)
        allowances = {}
        allowance_fields = {
            "hra": user_row.get("hra"),
            "transportation": user_row.get("transportation"),
            "other": user_row.get("other"),
            "mobile": user_row.get("mobile")
        }
        for key, val in allowance_fields.items():
            if val:
                allowances[key] = float(val)

    # Clean zero entries
    allowances = {k: v for k, v in allowances.items() if v}

    joining_date = user_row.get("joining_date")
    employment_status = user_row.get("employment_status")

    # 2. Determine salary period
    year = payroll_month.year
    month = payroll_month.month
    if not up_to_date:
        # Use the last day of the month
        if month == 12:
            up_to_date = date(year+1, 1, 1) - timedelta(days=1)
        else:
            up_to_date = date(year, month+1, 1) - timedelta(days=1)
    first_day = date(year, month, 1)
    days_in_period = (up_to_date - first_day).days + 1
    standard_month_days = 30  # Per UAE law

    # 3. Get total paid days, unpaid leave, sick leave, absences, etc. (PLUGGED BY HELPERS)
    # (actual_leaves, actual_unpaid, actual_sick, absent_days, etc.) 
    # TODO - implement below helpers deeply if needed
    unpaid_leave_days = await get_unpaid_leave_days(conn, user_id, first_day, up_to_date)
    sick_paid_15d, sick_next_30_half_d, sick_unpaid_d = await get_sick_leave_days_by_slabs(conn, user_id, first_day, up_to_date, joining_date)
    absence_days = await get_absence_days(conn, user_id, first_day, up_to_date)
    working_days = days_in_period - unpaid_leave_days - absence_days

    # 4. Calculate salary components
    # Net working days salary:
    net_working_days_salary = (basic_salary / standard_month_days) * working_days
    gross_salary = net_working_days_salary + sum(allowances.values()) if allowances else net_working_days_salary

    # 5. Calculate deductions
    unpaid_leave_deduction = (basic_salary / standard_month_days) * unpaid_leave_days
    sick_leave_halfpay_deduction = (basic_salary / standard_month_days * 0.5) * sick_next_30_half_d
    sick_leave_unpaid_deduction = (basic_salary / standard_month_days) * sick_unpaid_d
    absence_deduction = (basic_salary / standard_month_days) * absence_days
    # If penalties apply, multiply absence_deduction by 2
    absence_deduction_total = absence_deduction  # TODO: add penalty if required based on policy

    total_deductions = unpaid_leave_deduction + sick_leave_halfpay_deduction + sick_leave_unpaid_deduction + absence_deduction_total

    # 6. Net Pay
    net_pay = gross_salary - total_deductions

    # 7. EOSG Accrual (per law)
    eosg_accrual = calc_eosg_monthly_accrual(basic_salary, joining_date, up_to_date)

    # 8. Build breakdown
    return {
        "basic": basic_salary,
        "gross_pay": gross_salary,
        "total_deductions": total_deductions,
        "net_pay": net_pay,
        "allowances": allowances,
        "deductions": {
            "unpaid_leave": unpaid_leave_deduction,
            "sick_leave_halfpay": sick_leave_halfpay_deduction,
            "sick_leave_unpaid": sick_leave_unpaid_deduction,
            "absence": absence_deduction_total
        },
        "working_days": working_days,
        "unpaid_leave_days": unpaid_leave_days,
        "sick_leave_paid_15d": sick_paid_15d,
        "sick_leave_next_30_half": sick_next_30_half_d,
        "sick_leave_unpaid": sick_unpaid_d,
        "absence_days": absence_days,
        "eosg_monthly_accrual": eosg_accrual,
        "period_start": str(first_day),
        "period_end": str(up_to_date),
    }


async def upsert_payroll_item(conn, payroll_cycle_id, user_id, payroll_data: dict):
    # Insert or update payroll_items for this cycle/user
    await conn.execute("""
        INSERT INTO payroll_items (
            id, payroll_cycle_id, user_id, basic, allowances, gross_pay,
            total_deductions, net_pay, deductions, payslip_url, status, created_at, updated_at
        ) VALUES (
            gen_random_uuid(), $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, now(), now()
        )
        ON CONFLICT (payroll_cycle_id, user_id) DO UPDATE SET
            basic=excluded.basic,
            allowances=excluded.allowances,
            gross_pay=excluded.gross_pay,
            total_deductions=excluded.total_deductions,
            net_pay=excluded.net_pay,
            deductions=excluded.deductions,
            payslip_url=excluded.payslip_url,
            status=excluded.status,
            updated_at=now()
    """,
    payroll_cycle_id, user_id,
    payroll_data.get("basic"),
    payroll_data.get("allowances"),
    payroll_data.get("gross_pay"),
    payroll_data.get("total_deductions"),
    payroll_data.get("net_pay"),
    payroll_data.get("deductions"),  # Should be JSON
    None,  # payslip_url not created yet
    "pending"
    )

async def find_or_create_payroll_cycle(conn, year, month):
    # Returns payroll_cycle_id for month, creates if not exists
    mdate = date(year, month, 1)
    row = await conn.fetchrow("SELECT id FROM payroll_cycles WHERE month=$1", mdate)
    if not row:
        row = await conn.fetchrow("""
            INSERT INTO payroll_cycles (month, status, created_at) VALUES ($1, 'draft', now()) RETURNING id
        """, mdate)
    return row["id"]

async def run_monthly_payroll(conn, payroll_month: date):
    # Get all active employees
    employees = await conn.fetch("SELECT id FROM users WHERE is_active=true AND employment_status='permanent'")
    year = payroll_month.year
    month = payroll_month.month
    payroll_cycle_id = await find_or_create_payroll_cycle(conn, year, month)
    for e in employees:
        result = await calculate_employee_payroll(conn, e["id"], payroll_month)
        await upsert_payroll_item(conn, payroll_cycle_id, e["id"], result)

async def payroll_preview(conn, user_id: str, payroll_month: date, up_to_date: date = None):
    # API helper - just calculate and return, do not update DB
    return await calculate_employee_payroll(conn, user_id, payroll_month, up_to_date)

# ----------- Helper functions below (just stubs, to fill as next step) ------------
async def get_unpaid_leave_days(conn, user_id, date_from, date_to):
    # Get all approved unpaid leaves overlapping date range, sum days inside range
    rows = await conn.fetch("""
        SELECT start_date, end_date, half_day, half_day_slot
        FROM leaves
        WHERE user_id=$1 AND status='approved' AND leave_type_id=(SELECT id FROM leave_types WHERE name ILIKE 'unpaid')
              AND end_date >= $2
              AND start_date <= $3
    """, user_id, date_from, date_to)
    total_days = 0.0
    for r in rows:
        s = max(r["start_date"], date_from)
        e = min(r["end_date"], date_to)
        days = (e - s).days + 1
        if r["half_day"]:
            days = max(0.5, days - 0.5)
        total_days += float(days)
    return total_days

async def get_sick_leave_days_by_slabs(conn, user_id, date_from, date_to, joining_date):
    # UAE Law: 0-15: full, 16-45: half-pay, >45: unpaid; probation: none
    # Find all sick leaves, accumulate slabs across all
    # This implementation assumes all sick leaves in the period are post-probation (or probation disables all)
    user = await conn.fetchrow("SELECT employment_status, joining_date FROM users WHERE id=$1", user_id)
    if not user:
        return 0,0,0
    # UAE: no paid sick leave in probation (6 months from joining)
    if user["employment_status"].lower() == "probation" and (datetime.now().date() - (user["joining_date"] or datetime.now().date())).days < 180:
        # Sick leave not eligible
        return 0,0,0
    rows = await conn.fetch("""
        SELECT start_date, end_date, half_day, half_day_slot
        FROM leaves
        WHERE user_id=$1 AND status='approved' AND leave_type_id=(SELECT id FROM leave_types WHERE name ILIKE 'sick')
              AND end_date >= $2
              AND start_date <= $3
    """, user_id, date_from, date_to)
    # Flat list of days
    sick_dates = []
    for r in rows:
        s = max(r["start_date"], date_from)
        e = min(r["end_date"], date_to)
        days = (e - s).days + 1
        for i in range(days):
            day = s + timedelta(days=i)
            if r["half_day"]:
                sick_dates.append((day, 0.5))
            else:
                sick_dates.append((day, 1.0))
    # Sort by date; apply slabs
    sick_dates.sort()
    sick_paid_15d = sick_next_30_half_d = sick_unpaid_d = 0.0
    sick_days_used = 0.0
    for _, d in sick_dates:
        sick_days_used += d
        if sick_days_used <= 15:
            sick_paid_15d += d
        elif sick_days_used <= 45:
            remain_half = min(45 - (sick_days_used - d), d)
            sick_next_30_half_d += remain_half
            spill = d - remain_half
            sick_unpaid_d += spill
        else:
            sick_unpaid_d += d
    sick_paid_15d = min(sick_paid_15d, 15)
    sick_next_30_half_d = min(sick_next_30_half_d, 30)
    return sick_paid_15d, sick_next_30_half_d, sick_unpaid_d

async def get_absence_days(conn, user_id, date_from, date_to):
    # For now: days in range not covered by approved leave (annual/unpaid/sick) and not present in attendance, and not a weekend (Fri/Sat)
    # Not accounting holidays for this simple version
    # 1. Build set of all days in range
    d = date_from
    total_absent = 0
    while d <= date_to:
        # Friday/Saturday as weekend
        if d.weekday() in (4,5):  # Friday=4, Saturday=5 (Python: Mon=0)
            d += timedelta(days=1)
            continue
        # If covered by leave
        leave = await conn.fetchval("""
            SELECT 1 FROM leaves WHERE user_id=$1 AND status='approved' AND start_date <= $2 AND end_date >= $2
        """, user_id, d)
        if leave:
            d += timedelta(days=1)
            continue
        # If present in attendance (clock_in for day exists)
        att = await conn.fetchval("""
            SELECT 1 FROM attendance WHERE user_id=$1 AND date_trunc('day', clock_in_at)=$2
        """, user_id, d)
        if att:
            d += timedelta(days=1)
            continue
        total_absent += 1
        d += timedelta(days=1)
    return total_absent

def calc_eosg_monthly_accrual(basic_salary, joining_date, up_to_date):
    # For first 5 years: (Basic ÷ 30) × 21 ÷ 12
    # Beyond:           (Basic ÷ 30) × 30 ÷ 12
    if not joining_date or not basic_salary:
        return 0.0
    # How many years/months of service?
    years = (up_to_date - joining_date).days / 365.25
    if years < 5:
        return (basic_salary / 30) * 21 / 12
    else:
        return (basic_salary / 30) * 30 / 12
