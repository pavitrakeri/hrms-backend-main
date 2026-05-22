# Celery Setup for HRMS Scheduled Tasks

## Overview
This HRMS uses Celery for background task processing and scheduled jobs, including:
- **Monthly Leave Accrual** (1st of every month at 2 AM)
- **Year-end Leave Carry Forward** (1st January at 3 AM)
- **Monthly Payroll Calculation** (25th of every month at 2 AM)

## Prerequisites
- Redis server running (configured in `app/tasks.py`)
- Python environment with all dependencies installed

## Running Celery Worker

The Celery worker processes background tasks:

```bash
# Start the Celery worker
celery -A app.tasks.celery_app worker --loglevel=info
```

## Running Celery Beat Scheduler

Celery Beat is required to trigger scheduled tasks at their defined times:

```bash
# Start the Celery beat scheduler
celery -A app.tasks.celery_app beat --loglevel=info
```

## Running Both Together (Development)

For development, you can run both worker and beat in a single command:

```bash
celery -A app.tasks.celery_app worker --beat --loglevel=info
```

## Production Deployment

For production, run worker and beat as separate processes:

### Terminal 1 - Worker
```bash
celery -A app.tasks.celery_app worker --loglevel=info --concurrency=4
```

### Terminal 2 - Beat Scheduler
```bash
celery -A app.tasks.celery_app beat --loglevel=info
```

## Scheduled Tasks

### 1. Monthly Leave Accrual
- **Task Name**: `accrue_annual_leave`
- **Schedule**: 1st of every month at 2:00 AM UTC
- **Purpose**: Adds 2.5 days of annual leave to all permanent employees
- **Location**: `app/features/leave_accrual.py`

### 2. Year-end Carry Forward
- **Task Name**: `carry_forward_annual_leave`
- **Schedule**: January 1st at 3:00 AM UTC
- **Purpose**: Carries forward unused annual leave (max 60 days) to next year
- **Location**: `app/features/leave_accrual.py`

### 3. Monthly Payroll Calculation
- **Task Name**: `run_monthly_payroll`
- **Schedule**: 25th of every month at 2:00 AM UTC
- **Purpose**: Calculates payroll for all active employees for the current month
- **Location**: `app/features/payroll/payroll_tasks.py`
- **What it does**:
  - Fetches all active permanent employees
  - Calculates salary components (basic, allowances, deductions)
  - Applies UAE labor law rules for sick leave, unpaid leave, absences
  - Calculates EOSG (gratuity) monthly accrual
  - Writes/updates `payroll_items` table for the current month

## Manual Payroll Trigger

If you need to run payroll manually (outside the schedule):

### Via API
```bash
POST /payroll/admin/run-payroll?month=2025-10-01
Authorization: Bearer <admin_token>
```

Only Admin, Finance, or CFO roles can trigger manual payroll runs.

### Via Python
```python
from app.features.payroll.payroll_calculator import run_monthly_payroll
from app.db import get_db_pool
from datetime import date

async def manual_payroll():
    db_pool = get_db_pool()
    async with db_pool.acquire() as conn:
        await run_monthly_payroll(conn, date(2025, 10, 1))
```

## Monitoring

### Check Celery Worker Status
```bash
celery -A app.tasks.celery_app inspect active
```

### Check Scheduled Tasks
```bash
celery -A app.tasks.celery_app inspect scheduled
```

### Check Registered Tasks
```bash
celery -A app.tasks.celery_app inspect registered
```

## Troubleshooting

### Tasks not running
1. Ensure Redis is running and accessible
2. Check that both worker and beat are running
3. Verify timezone settings in `app/tasks.py`
4. Check logs for errors

### Payroll calculation issues
1. Check that `payroll_cycles` table has entries
2. Verify employee salary data in `users` table
3. Check leave and attendance data is properly recorded
4. Review logs in Celery worker output

## Environment Variables

```bash
REDIS_URL=redis://your-redis-host:port
```

## Docker Deployment (Optional)

Add to your `docker-compose.yml`:

```yaml
celery_worker:
  build: .
  command: celery -A app.tasks.celery_app worker --loglevel=info
  depends_on:
    - redis
    - db
  environment:
    - REDIS_URL=redis://redis:6379

celery_beat:
  build: .
  command: celery -A app.tasks.celery_app beat --loglevel=info
  depends_on:
    - redis
  environment:
    - REDIS_URL=redis://redis:6379
```

