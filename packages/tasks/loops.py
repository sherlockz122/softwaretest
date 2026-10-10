"""API 容器内的后台任务循环：outbox 投递与恢复协调（由 lifespan 启动，可配置关闭）。"""

import logging
import threading

from packages.platform.config import Settings
from packages.tasks.coordinator import run_coordinator_cycle
from packages.tasks.dispatcher import run_dispatch_cycle

logger = logging.getLogger("defectguard")


def start_task_loops(session_factory, settings: Settings, celery_app):
    """启动投递器与协调器守护线程，返回 (stop_event, threads)。"""
    stop_event = threading.Event()

    def dispatch_loop():
        while not stop_event.wait(settings.task_loop_interval_seconds):
            try:
                run_dispatch_cycle(session_factory, settings, celery_app)
            except Exception:
                logger.exception("TASK_DISPATCH_LOOP_ERROR")

    def coordinator_loop():
        while not stop_event.wait(settings.task_coordinator_interval_seconds):
            try:
                run_coordinator_cycle(session_factory, settings)
            except Exception:
                logger.exception("TASK_COORDINATOR_LOOP_ERROR")

    threads = [
        threading.Thread(target=dispatch_loop, name="task-dispatch", daemon=True),
        threading.Thread(target=coordinator_loop, name="task-coordinator", daemon=True),
    ]
    for thread in threads:
        thread.start()
    return stop_event, threads


def stop_task_loops(stop_event, threads) -> None:
    stop_event.set()
    for thread in threads:
        thread.join(timeout=2)
