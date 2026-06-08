from fastapi import HTTPException
from datetime import datetime

async def initiate_payroll(conn, user, req):
    """
    Finance or CFO can initiate payroll setup for an employee.
    """
    # ✅ Check role permissions
    role = await conn.fetchval("""
        SELECT r.name FROM roles r
        JOIN users u ON u.role_id = r.id
        WHERE u.id=$1
    """, user["id"])

    if role not in ("finance", "cfo", "admin"):
        raise HTTPException(status_code=403, detail="Only Finance, CFO, or Admin can initiate payroll")

    # ✅ Resolve employee by email
    emp = await conn.fetchrow("SELECT id, full_name, email FROM users WHERE LOWER(email)=LOWER($1)", req.employee_email)
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    employee_id = emp["id"]

    # ✅ Calculate gross salaries
    gross_monthly = req.basic_salary + req.hra + req.allowances + req.other_benefits
    gross_annual = gross_monthly * 12

    # ✅ Check if payroll already exists
    exists = await conn.fetchval("""
        SELECT 1 FROM employee_payroll_setup WHERE employee_id=$1
    """, employee_id)
    if exists:
        raise HTTPException(status_code=400, detail="Payroll already initiated for this employee")

    # ✅ Insert into payroll setup
    row = await conn.fetchrow("""
        INSERT INTO employee_payroll_setup (
            employee_id, employee_email, basic_salary, hra, allowances, other_benefits,
            gross_monthly, gross_annual, payment_mode, bank_account_number,
            bank_name, iban_number, remarks, created_by
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
        RETURNING id
    """,
    employee_id, emp["email"], req.basic_salary, req.hra, req.allowances, req.other_benefits,
    gross_monthly, gross_annual, req.payment_mode, req.bank_account_number,
    req.bank_name, req.iban_number, req.remarks, user["id"])

    return {
        "status": "success",
        "message": f"Payroll initiated for {emp['full_name']} (Monthly: {gross_monthly})",
        "payroll_id": str(row["id"])
    }


async def update_payroll_details(conn, user, employee_email: str, req):
    """
    Finance or CFO can update payroll structure for an employee using their email.
    """
    # ✅ Check role permissions
    role = await conn.fetchval("""
        SELECT r.name FROM roles r
        JOIN users u ON u.role_id = r.id
        WHERE u.id=$1
    """, user["id"])

    if role not in ("finance", "cfo", "admin"):
        raise HTTPException(status_code=403, detail="Only Finance, CFO, or Admin can update payroll details")

    # ✅ Resolve employee via email
    emp = await conn.fetchrow("SELECT id, email FROM users WHERE LOWER(email)=LOWER($1)", employee_email)
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    # ✅ Check if payroll exists
    payroll = await conn.fetchrow("""
        SELECT * FROM employee_payroll_setup WHERE employee_id=$1
    """, emp["id"])
    if not payroll:
        raise HTTPException(status_code=404, detail="No payroll record found for this employee")

    # ✅ Prepare updated values
    updated_basic = req.basic_salary if req.basic_salary is not None else payroll["basic_salary"]
    updated_hra = req.hra if req.hra is not None else payroll["hra"]
    updated_allowances = req.allowances if req.allowances is not None else payroll["allowances"]
    updated_benefits = req.other_benefits if req.other_benefits is not None else payroll["other_benefits"]

    gross_monthly = updated_basic + updated_hra + updated_allowances + updated_benefits
    gross_annual = gross_monthly * 12

    # ✅ Update record
    await conn.execute("""
        UPDATE employee_payroll_setup
        SET
            basic_salary = $1,
            hra = $2,
            allowances = $3,
            other_benefits = $4,
            gross_monthly = $5,
            gross_annual = $6,
            payment_mode = COALESCE($7, payment_mode),
            bank_account_number = COALESCE($8, bank_account_number),
            bank_name = COALESCE($9, bank_name),
            iban_number = COALESCE($10, iban_number),
            remarks = COALESCE($11, remarks),
            updated_at = now()
        WHERE employee_id = $12
    """,
    updated_basic, updated_hra, updated_allowances, updated_benefits,
    gross_monthly, gross_annual,
    req.payment_mode, req.bank_account_number, req.bank_name,
    req.iban_number, req.remarks, emp["id"])

    return {
        "status": "success",
        "message": f"Payroll details updated for employee {emp['email']}",
        "gross_monthly": gross_monthly,
        "gross_annual": gross_annual
    }
