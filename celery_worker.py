from app.tasks import celery_app

if __name__ == "__main__":
    celery_app.worker_main(argv=["worker", "--loglevel=info"])



# celery -A app.tasks.celery_app worker --loglevel=info