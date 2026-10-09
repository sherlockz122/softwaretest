"""Real Celery process foundation; business execution arrives with outbox tasks."""

from celery import Celery

from packages.platform.config import load_settings

settings = load_settings()
app = Celery("defectguard", broker=settings.broker_url)
app.conf.update(
    task_default_queue="defectguard",
    accept_content=["json"],
    task_serializer="json",
    result_serializer="json",
    task_ignore_result=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    timezone="UTC",
    enable_utc=True,
    broker_transport_options={"global_keyprefix": "defectguard:"},
)
