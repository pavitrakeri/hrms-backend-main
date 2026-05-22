from celery.schedules import crontab
from app.tasks import celery_app

celery_app.conf.beat_schedule = {
    "monthly-annual-accrual": {
        "task": "accrue_annual_leave",
        "schedule": crontab(day_of_month=1, hour=0, minute=0),  # 1st of every month
    },
    "year-end-carry-forward": {
        "task": "carry_forward_annual_leave",
        "schedule": crontab(month_of_year=12, day_of_month=31, hour=0, minute=0),
    },
}

celery_app.conf.timezone = "UTC"

if __name__ == "__main__":
    celery_app.start()
