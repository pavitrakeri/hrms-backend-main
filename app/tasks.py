from celery import Celery
from celery.schedules import crontab
import os

REDIS_URL = os.getenv("REDIS_URL", "redis://default:AMD7lST1olkJcG9cf4KeWdxd05wuEmau@redis-14824.c321.us-east-1-2.ec2.redns.redis-cloud.com:14824")

celery_app = Celery("hrms_tasks", broker=REDIS_URL, backend=REDIS_URL)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)

# Celery Beat Schedule for periodic tasks
celery_app.conf.beat_schedule = {
    # Monthly leave accrual (1st of every month at 2 AM)
    'accrue-monthly-leaves': {
        'task': 'accrue_annual_leave',
        'schedule': crontab(hour=2, minute=0, day_of_month=1),
    },
    # Year-end carry forward (1st January at 3 AM)
    'carry-forward-leaves': {
        'task': 'carry_forward_annual_leave',
        'schedule': crontab(hour=3, minute=0, day_of_month=1, month_of_year=1),
    },
    # Monthly payroll calculation (25th of every month at 2 AM)
    'run-monthly-payroll': {
        'task': 'run_monthly_payroll',
        'schedule': crontab(hour=2, minute=0, day_of_month=25),
    },
}
