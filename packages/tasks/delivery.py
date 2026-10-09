"""At-least-once delivery with short DB claims and fenced write-back."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from packages.persistence.models import AsyncTask, TaskOutbox
from packages.tasks.service import TaskService, audit, database_now


class Dispatcher:
    def __init__(self, settings, connections, publisher):
        self.settings, self.engine, self.publisher = settings, connections.engine, publisher

    def claim(self):
        with Session(self.engine) as db, db.begin():
            timestamp = database_now(db)
            row = db.scalar(
                select(TaskOutbox)
                .where(
                    or_(
                        (TaskOutbox.status == "pending")
                        & (TaskOutbox.next_attempt_at <= timestamp),
                        (TaskOutbox.status == "dispatching")
                        & (TaskOutbox.lease_until <= timestamp),
                    )
                )
                .order_by(TaskOutbox.next_attempt_at, TaskOutbox.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return None
            task = db.scalar(select(AsyncTask).where(AsyncTask.id == row.task_id).with_for_update())
            if task.status != "queued":
                row.status, row.last_error_code = "dead", "TASK_NOT_QUEUED"
                return None
            if row.attempts >= self.settings.delivery_max_attempts:
                self.dead(db, row, task)
                return None
            row.status, row.delivery_token = "dispatching", str(uuid4())
            row.attempts += 1
            row.lease_until = timestamp + timedelta(seconds=self.settings.delivery_lease_seconds)
            return row.id, row.task_id, row.delivery_token

    @staticmethod
    def dead(db, row, task):
        row.status, row.last_error_code, row.lease_until = "dead", "TASK_DISPATCH_FAILED", None
        if task.status == "queued":
            TaskService.terminal(db, task, "failed", "TASK_DISPATCH_FAILED")
            audit(db, task, "task.failed")

    def finish(self, event_id, token, sent):
        with Session(self.engine) as db, db.begin():
            row = db.scalar(select(TaskOutbox).where(TaskOutbox.id == event_id).with_for_update())
            timestamp = database_now(db)
            if (
                not row
                or row.status != "dispatching"
                or row.delivery_token != token
                or row.lease_until <= timestamp
            ):
                return False
            if sent:
                row.status, row.sent_at, row.lease_until = "sent", timestamp, None
                row.last_error_code = None
            elif row.attempts >= self.settings.delivery_max_attempts:
                task = db.scalar(
                    select(AsyncTask).where(AsyncTask.id == row.task_id).with_for_update()
                )
                self.dead(db, row, task)
            else:
                row.status, row.last_error_code, row.lease_until = (
                    "pending",
                    "TASK_BROKER_UNAVAILABLE",
                    None,
                )
                row.next_attempt_at = timestamp + timedelta(
                    seconds=min(5 * 2 ** (row.attempts - 1), 60)
                )
            return True

    def dispatch_once(self):
        self.publisher_failed = False
        claim = self.claim()
        if claim is None:
            return False
        event_id, task_id, token = claim
        # No DB transaction remains open during broker I/O. Errors are never stored verbatim.
        try:
            self.publisher(task_id)
        except Exception:
            self.publisher_failed = True
            self.finish(event_id, token, False)
        else:
            self.finish(event_id, token, True)
        return True
