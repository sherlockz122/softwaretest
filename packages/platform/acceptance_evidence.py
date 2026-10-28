"""Strict task-only evidence for disposable acceptance, with no payloads or messages."""

import json
import re
from uuid import UUID

VERSION = "fresh-task-evidence-v1"
LIMIT = 1000
MAX_BYTES = 256 * 1024
TYPES = {
    "diagnostic",
    "dataset.build",
    "model.train",
    "repository.clone",
    "repository.parse",
    "repository.sync",
    "repository.fix",
    "repository.szz",
    "repository.feature",
}
STATES = {"queued", "running", "cancel_requested", "succeeded", "failed", "cancelled"}
FIELDS = {"id", "root_task_id", "type", "status", "error_code"}


def validate(value):
    if not isinstance(value, dict) or set(value) != {"version", "truncated", "tasks"}:
        raise ValueError("TASK_EVIDENCE_SCHEMA")
    if value["version"] != VERSION or type(value["truncated"]) is not bool:
        raise ValueError("TASK_EVIDENCE_VERSION")
    rows = value["tasks"]
    if not isinstance(rows, list) or len(rows) > LIMIT:
        raise ValueError("TASK_EVIDENCE_COUNT")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != FIELDS:
            raise ValueError("TASK_EVIDENCE_FIELDS")
        for name in ("id", "root_task_id"):
            if not isinstance(row[name], str) or str(UUID(row[name])) != row[name]:
                raise ValueError("TASK_EVIDENCE_ID")
        if row["id"] in seen or row["type"] not in TYPES or row["status"] not in STATES:
            raise ValueError("TASK_EVIDENCE_VALUE")
        seen.add(row["id"])
        code = row["error_code"]
        if code is not None and (
            not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code)
        ):
            raise ValueError("TASK_EVIDENCE_CODE")
    return value


def decode(body):
    if len(body) > MAX_BYTES:
        raise ValueError("TASK_EVIDENCE_OUTPUT_LIMIT")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("TASK_EVIDENCE_DUPLICATE_KEY")
            result[key] = value
        return result

    return validate(json.loads(body.decode("ascii"), object_pairs_hook=pairs))


def collect_database():
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from packages.persistence.models import AsyncTask
    from packages.platform.config import load_settings
    from packages.platform.connections import Connections

    connections = Connections(load_settings())
    try:
        with Session(connections.engine) as db:
            rows = (
                db.execute(
                    select(
                        AsyncTask.id,
                        AsyncTask.root_task_id,
                        AsyncTask.type,
                        AsyncTask.status,
                        AsyncTask.error_code,
                    )
                    .order_by(AsyncTask.id)
                    .limit(LIMIT + 1)
                )
                .mappings()
                .all()
            )
            return validate(
                {
                    "version": VERSION,
                    "truncated": len(rows) > LIMIT,
                    "tasks": [dict(r) for r in rows[:LIMIT]],
                }
            )
    finally:
        connections.close()
