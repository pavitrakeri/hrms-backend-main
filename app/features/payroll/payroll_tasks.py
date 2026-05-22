from datetime import datetime, date
from app.db import get_db_pool
from app.tasks import celery_app
from app.features.payroll.payroll_calculator import run_monthly_payroll


@celery_app.task(name="run_monthly_payroll")
async def run_monthly_payroll_task():
    """
    Celery task to run monthly payroll calculation for all employees.
    Scheduled to run on the 25th of each month at 2 AM.
    Calculates payroll for the current month and writes to payroll_items.
    """
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        try:
            # Get current month
            today = date.today()
            payroll_month = date(today.year, today.month, 1)
            
            print(f"[Celery] Starting monthly payroll calculation for {payroll_month.strftime('%Y-%m')}...")
            
            # Run the payroll calculation
            await run_monthly_payroll(conn, payroll_month)
            
            print(f"[Celery] Monthly payroll calculation completed successfully for {payroll_month.strftime('%Y-%m')} ✅")
            
            return {"status": "success", "month": payroll_month.strftime('%Y-%m')}
            
        except Exception as e:
            print(f"[Celery Error run_monthly_payroll] {e}")
            return {"status": "error", "message": str(e)}

