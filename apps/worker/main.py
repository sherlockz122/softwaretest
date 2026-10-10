"""Real Celery process foundation; business execution arrives with outbox tasks."""

from celery import Celery

from packages.platform.config import Settings, load_settings
from packages.tasks.domain import EXECUTE_TASK_NAME


def create_celery_app(settings: Settings) -> Celery:
    app = Celery("defectguard", broker=settings.broker_url)
    app.conf.update(
        include=["apps.worker.tasks"],
        task_default_queue="defectguard",
        accept_content=["json"],
        task_serializer="json",
        result_serializer="json",
        task_ignore_result=True,
        worker_prefetch_multiplier=1,
        broker_connection_retry_on_startup=True,
        timezone="UTC",
        enable_utc=True,
        broker_transport_options={
            "global_keyprefix": "defectguard:",
            "socket_connect_timeout": 3,
        },
    )
    return app


settings = load_settings()
app = create_celery_app(settings)

__all__ = ["app", "settings", "EXECUTE_TASK_NAME", "create_celery_app"]
