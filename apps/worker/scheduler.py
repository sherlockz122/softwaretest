"""Dedicated dispatcher/coordinator; no Celery Beat or in-memory task authority."""

import logging
import signal
import time

from apps.worker.main import app, settings
from packages.platform.connections import Connections
from packages.tasks.delivery import Dispatcher
from packages.tasks.service import TaskService

logger = logging.getLogger("defectguard.scheduler")


def publish(task_id):
    app.send_task("defectguard.execute", args=[task_id], queue="defectguard", retry=False)


def main():
    connections = Connections(settings)
    connections.require_schema()
    dispatcher = Dispatcher(settings, connections, publish)
    tasks = TaskService(settings, connections)
    running = True

    def stop(*args):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    next_scan = 0
    try:
        while running:
            try:
                if time.monotonic() >= next_scan:
                    tasks.reconcile()
                    next_scan = time.monotonic() + settings.coordinator_interval_seconds
                # Bound each poll; a disconnected broker must not starve lease recovery.
                for _ in range(10):
                    if not running or not dispatcher.dispatch_once() or dispatcher.publisher_failed:
                        break
                connections.redis.set("defectguard:scheduler:heartbeat", "ok", ex=60)
            except Exception:
                logger.error("SYSTEM_SCHEDULER_DEPENDENCY_UNAVAILABLE")
            time.sleep(settings.scheduler_interval_seconds)
    finally:
        connections.close()


if __name__ == "__main__":
    main()
