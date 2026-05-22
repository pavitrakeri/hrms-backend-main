from datetime import datetime
from app.db import get_db_pool
from app.tasks import celery_app

@celery_app.task(name="accrue_annual_leave")
async def accrue_annual_leave():
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        try:
            leave_type_id = await conn.fetchval("SELECT id FROM leave_types WHERE name ILIKE 'annual'")
            employees = await conn.fetch("""
                SELECT id FROM users
                WHERE is_active=true AND employment_status='permanent'
            """)
            for emp in employees:
                lb = await conn.fetchrow("""
                    SELECT id, total_entitled, remaining
                    FROM leave_balance
                    WHERE user_id=$1 AND leave_type_id=$2 AND year=EXTRACT(YEAR FROM now())
                """, emp["id"], leave_type_id)
                if lb:
                    remaining = float(lb["remaining"] or 0)
                    if remaining < 60:
                        await conn.execute("""
                            UPDATE leave_balance
                            SET total_entitled=total_entitled+2.5, last_updated=now()
                            WHERE id=$1
                        """, lb["id"])
                else:
                    await conn.execute("""
                        INSERT INTO leave_balance (user_id, leave_type_id, year, total_entitled)
                        VALUES ($1, $2, EXTRACT(YEAR FROM now()), 2.5)
                    """, emp["id"], leave_type_id)
            print("[Celery] Monthly leave accrual completed ✅")
        except Exception as e:
            print(f"[Celery Error accrue_annual_leave] {e}")


@celery_app.task(name="carry_forward_annual_leave")
async def carry_forward_annual_leave():
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        try:
            leave_type_id = await conn.fetchval("SELECT id FROM leave_types WHERE name ILIKE 'annual'")
            balances = await conn.fetch("""
                SELECT user_id, remaining FROM leave_balance
                WHERE leave_type_id=$1 AND year=EXTRACT(YEAR FROM now())
            """, leave_type_id)
            for b in balances:
                carry = min(float(b["remaining"] or 0), 60)
                next_year = datetime.now().year + 1
                await conn.execute("""
                    INSERT INTO leave_balance (user_id, leave_type_id, year, carried_forward)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (user_id, leave_type_id, year) DO NOTHING
                """, b["user_id"], leave_type_id, next_year, carry)
            print("[Celery] Year-end carry forward done ✅")
        except Exception as e:
            print(f"[Celery Error carry_forward_annual_leave] {e}")
