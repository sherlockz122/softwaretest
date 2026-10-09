"""Real worker with fenced database execution and at-least-once delivery."""

from celery import Celery

from packages.platform.config import load_settings
from packages.platform.worker_logging import protect_broker_logs  # noqa: F401

settings = load_settings()
app = Celery("defectguard", broker=settings.broker_url, include=["apps.worker.tasks"])
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
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_publish_retry=False,
    broker_connection_timeout=2,
    worker_cancel_long_running_tasks_on_connection_loss=True,
    broker_transport_options={
        "global_keyprefix": "defectguard:",
        "socket_connect_timeout": 2,
        "socket_timeout": 2,
        "retry_on_timeout": False,
    },
)
