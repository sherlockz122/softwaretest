import logging
import time
from uuid import UUID

from apps.worker.main import app, settings
from packages.mining.fix import FixService
from packages.mining.szz import SZZService
from packages.platform.connections import Connections
from packages.repositories.clone import CloneExecutor
from packages.repositories.parser import ParseExecutor
from packages.repositories.parsing import ParsingService
from packages.repositories.service import RepositoryService
from packages.repositories.sync import SyncExecutor, SyncService
from packages.tasks.service import TaskService


@app.task(name="defectguard.execute")
def execute(task_id):
    try:
        task_id = str(UUID(task_id))
    except (ValueError, TypeError, AttributeError):
        return
    connections = Connections(settings)
    service = TaskService(settings, connections)
    try:
        claim = service.claim(task_id)
        if claim is None:
            return
        token, duration = claim
        if isinstance(duration, dict):
            if "szz_version" in duration:
                ParseExecutor(settings, SZZService(settings, connections)).run(
                    task_id, token, duration
                )
            elif "fix_version" in duration:
                ParseExecutor(settings, FixService(settings, connections)).run(
                    task_id, token, duration
                )
            elif "sync_version" in duration:
                SyncExecutor(settings, SyncService(settings, connections)).run(
                    task_id, token, duration
                )
            elif "parser_version" in duration:
                ParseExecutor(settings, ParsingService(settings, connections)).run(
                    task_id, token, duration
                )
            else:
                CloneExecutor(settings, RepositoryService(settings, connections)).run(
                    task_id, token, duration
                )
            return
        started = time.monotonic()
        while True:
            elapsed = time.monotonic() - started
            complete = elapsed >= duration
            if (
                not service.checkpoint(
                    task_id,
                    token,
                    progress=100 * min(elapsed / max(duration, 1), 1),
                    processed=int(elapsed),
                    complete=complete,
                )
                or complete
            ):
                return
            time.sleep(min(settings.task_heartbeat_seconds, max(0.01, duration - elapsed)))
    except Exception:
        # Leave persisted running lease to recovery; never log driver/credential details.
        logging.getLogger("defectguard.worker").error(
            "TASK_EXECUTION_INTERRUPTED task_id=%s", task_id
        )
    finally:
        connections.close()
