# ruff: noqa: F811
"""Measure actual MySQL freeze transactions and prove Git runs outside row locks."""

import json
import os
import time
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from packages.persistence.models import AsyncTask, FeatureItem, FeatureRun, Repository
from packages.repositories.parser import NativeReader, ParseExecutor
from packages.tasks.service import database_now
from scripts.probe_causal_features import ROOT, checked_path
from tests.test_feature_v2 import start
from tests.test_szz import execute, szz_env  # noqa: F401
from tests.test_tasks import expire, task_env  # noqa: F401

pytestmark = pytest.mark.integration


def test_actual_queries_freeze_transaction_and_git_lock_release(szz_env, monkeypatch):
    settings, connections, _, _, repo, *_ = szz_env
    service, root = start(szz_env)
    engine = connections.engine
    queries, locked_transactions, freeze_transactions = [], [], []
    transactions = {}
    native_checks = 0

    def begin(db):
        transactions[id(db)] = {"started": time.perf_counter(), "fenced": False}

    def finish(db):
        data = transactions.pop(id(db), None)
        if data and data["fenced"]:
            locked_transactions.append(time.perf_counter() - data["started"])
            if data.get("freeze"):
                freeze_transactions.append(time.perf_counter() - data["started"])

    def statement(db, cursor, sql, parameters, context, executemany):
        lowered = sql.lower()
        if "from file_change" in lowered and lowered.startswith("select"):
            queries.append("files")
        if "for update" in lowered and "nowait" not in lowered and "from async_task" in lowered:
            transactions[id(db)]["fenced"] = True
        if lowered.startswith("insert into feature_item"):
            transactions[id(db)]["freeze"] = True

    original = NativeReader.record

    def record(reader, sha):
        nonlocal native_checks
        # Another connection can acquire both rows immediately during Git reads.
        with Session(engine) as db, db.begin():
            assert db.scalar(
                select(Repository).where(Repository.id == repo).with_for_update(nowait=True)
            )
            assert db.scalar(
                select(AsyncTask).where(AsyncTask.id == root).with_for_update(nowait=True)
            )
        native_checks += 1
        return original(reader, sha)

    monkeypatch.setattr(NativeReader, "record", record)
    listeners = (
        ("begin", begin),
        ("commit", finish),
        ("rollback", finish),
        ("before_cursor_execute", statement),
    )
    for name, callback in listeners:
        event.listen(engine, name, callback)
    try:
        assert execute(service, settings, root)
    finally:
        for name, callback in listeners:
            event.remove(engine, name, callback)
    with Session(engine) as db:
        point = db.get(FeatureRun, root)
        count = db.scalar(
            select(func.count()).select_from(FeatureItem).where(FeatureItem.root_task_id == root)
        )
        assert count == point.processed == 14 and point.input_digest and point.quality_json
    assert native_checks == 14 and locked_transactions
    assert len(queries) == 1 and len(freeze_transactions) == 1
    # A regression in query shape is checked separately from machine-dependent timing.
    result = {
        "commits": count,
        "file_select_queries": len(queries),
        "git_lock_release_checks": native_checks,
        "fenced_transaction_count": len(locked_transactions),
        "fenced_transaction_max_seconds": max(locked_transactions),
        "freeze_transaction_seconds": freeze_transactions[0],
        "scope": "actual isolated MySQL/Git fixture; timings not full production throughput",
    }
    if filename := os.environ.get("DG_FEATURE_READ_EVIDENCE"):
        path = checked_path(Path(filename))
        assert path.is_relative_to(ROOT / "runtime/acceptance") and not path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


@pytest.mark.parametrize("failure", ["write", "expired"])
def test_freeze_is_atomic_on_write_failure_or_expired_lease(szz_env, monkeypatch, failure):
    settings, connections, _, actors, *_ = szz_env
    service, root = start(szz_env)
    token, payload = service.tasks.claim(root)
    original = Session.flush
    interrupted = False

    def flush(db, *args, **kwargs):
        nonlocal interrupted
        freezing = any(
            isinstance(row, FeatureRun) and row.root_task_id == root and row.input_digest
            for row in db.dirty
        )
        result = original(db, *args, **kwargs)
        if freezing and not interrupted:
            interrupted = True
            if failure == "write":
                raise RuntimeError("freeze insert interrupted")
            task = db.get(AsyncTask, root)
            task.lease_until = database_now(db) - timedelta(seconds=1)
            original(db)
        return result

    monkeypatch.setattr(Session, "flush", flush)
    if failure == "write":
        with pytest.raises(RuntimeError, match="freeze insert interrupted"):
            ParseExecutor(settings, service).run(root, token, payload)
    else:
        assert not ParseExecutor(settings, service).run(root, token, payload)
    assert interrupted
    monkeypatch.undo()
    with Session(connections.engine) as db:
        point = db.get(FeatureRun, root)
        assert point.input_digest is None and point.source_digest is None
        assert point.processed == 0 and point.quality_json is None
        assert (
            db.scalar(
                select(func.count())
                .select_from(FeatureItem)
                .where(FeatureItem.root_task_id == root)
            )
            == 0
        )
    expire(connections.engine, "async_task", "lease_until", root)
    service.tasks.reconcile()
    with Session(connections.engine) as db:
        child = db.scalar(select(AsyncTask).where(AsyncTask.retry_of == root)).id
    assert not service.heartbeat(root, token)
    assert execute(service, settings, child)
    with Session(connections.engine) as db:
        assert db.get(FeatureRun, root).processed == 14
        assert db.get(FeatureRun, root).quality_json["count"] == 14


def test_atomic_freeze_storage_capacity_100_and_1000_rows(szz_env):
    settings, connections, _, actors, *_ = szz_env
    service, completed = start(szz_env)
    assert execute(service, settings, completed)
    with Session(connections.engine) as db:
        template = deepcopy(
            db.scalar(select(FeatureItem).where(FeatureItem.root_task_id == completed)).input_json
        )
    measurements = []
    for count in (100, 1000):
        service, root = start(szz_env)
        token, _ = service.tasks.claim(root)
        started = time.perf_counter()
        with pytest.raises(RuntimeError, match="storage probe rollback"):
            with Session(connections.engine) as db, db.begin():
                context = service.fenced(db, root, token)
                assert context
                for ordinal in range(count):
                    data = deepcopy(template)
                    sha = f"{ordinal + 1:040x}"
                    data["sha"] = data["commit"]["sha"] = data["message_purpose"]["sha"] = sha
                    db.add(
                        FeatureItem(
                            id=str(uuid4()),
                            root_task_id=root,
                            ordinal=ordinal,
                            sha=sha,
                            input_json=data,
                        )
                    )
                db.flush()
                assert database_now(db) < context[0].lease_until
                measurements.append(
                    {"rows": count, "locked_flush_seconds": time.perf_counter() - started}
                )
                raise RuntimeError("storage probe rollback")
        with Session(connections.engine) as db:
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(FeatureItem)
                    .where(FeatureItem.root_task_id == root)
                )
                == 0
            )
            assert db.get(FeatureRun, root).input_digest is None
        service.tasks.cancel(actors[1], root, str(uuid4()))
        assert not service.heartbeat(root, token)
    result = {
        "measurements": measurements,
        "scope": (
            "actual MySQL ORM atomic storage microprobe; repeated verified input shape "
            "with artificial SHAs, NOT native/source/feature throughput"
        ),
        "rolled_back_rows": True,
        "training_ready": False,
    }
    if filename := os.environ.get("DG_FEATURE_READ_EVIDENCE"):
        path = checked_path(Path(filename).with_name(Path(filename).stem + "-capacity.json"))
        assert path.is_relative_to(ROOT / "runtime/acceptance") and not path.exists()
        path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
