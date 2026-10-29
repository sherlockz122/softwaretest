"""Real Worker claim/kill/recovery; a DB fence traps short tasks without product hooks."""

import os
import time
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from packages.mining import public_assets as assets
from packages.persistence.models import AsyncTask, OperationLog
from packages.semantic import asset_catalog as catalog
from packages.semantic.verification import VerificationService
from tests.semantic_asset_support import source
from tests.test_tasks_runtime import docker, runtime_client, wait_ready, wait_task

pytestmark = [
    pytest.mark.runtime,
    pytest.mark.skipif(
        os.environ.get("DG_RUN_RUNTIME_TESTS") != "1",
        reason="explicit local fault-test opt-in required",
    ),
]


def test_real_semantic_worker_kill_successor_fixed_input_and_old_token_rejection():
    identities = catalog.catalog_ids()
    if not identities:
        # Disposable CI can start without an installed asset. This is explicitly
        # synthetic structural data; a workstation's existing real assets are reused.
        root = assets.ROOT / "runtime/temp" / ("semantic-runtime-" + uuid4().hex)
        root.mkdir(parents=True)
        directory, manifest = source(root)
        installed = catalog.install(directory, manifest["content_hash"])
        identities = [installed.name]
    identity = identities[0]
    before = {p.name: p.read_bytes() for p in (catalog.STORE / identity).iterdir()}
    held = None
    with runtime_client() as (api, settings, connections):
        deadline = time.monotonic() + 60
        while True:
            with Session(connections.engine) as db:
                active = db.scalar(
                    select(func.count())
                    .select_from(AsyncTask)
                    .where(AsyncTask.status.in_(["queued", "running", "cancel_requested"]))
                )
            if active == 0:
                break
            assert time.monotonic() < deadline, "existing tasks must settle before this own fault"
            time.sleep(0.1)
        response = api.post(
            f"/api/v1/semantic-assets/{identity}/verifications",
            headers={"Idempotency-Key": uuid4().hex},
        )
        assert response.status_code == 202
        task_id = response.json()["task_id"]
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                candidate = Session(connections.engine)
                try:
                    row = candidate.scalar(
                        select(AsyncTask)
                        .where(AsyncTask.id == task_id)
                        .with_for_update(nowait=True)
                    )
                    assert row.status not in {"succeeded", "failed", "cancelled"}, (
                        "must trap the actual Worker before terminal publication"
                    )
                    if row.status == "running":
                        held = candidate
                        old_token, payload = row.execution_token, dict(row.payload)
                        assert old_token and row.started_at and row.result_json is None
                        break
                except DBAPIError as exc:
                    if (
                        exc.orig.args[0] != 3572
                    ):  # MySQL NOWAIT conflict, never ignore other failures.
                        raise
                finally:
                    if held is not candidate:
                        candidate.rollback()
                        candidate.close()
                time.sleep(0.01)
            assert held is not None, "real Worker must commit a claim before SIGKILL"
            try:
                docker("kill", "--signal", "SIGKILL", "worker")
            finally:
                held.rollback()
                held.close()
                held = None
            failed = wait_task(api, task_id, {"failed"}, timeout=100)
            assert failed["error"]["code"] == "TASK_LEASE_EXPIRED"
            assert failed["result"] is None
            with Session(connections.engine) as db:
                successor = db.scalar(select(AsyncTask).where(AsyncTask.retry_of == task_id))
                assert successor is not None
                assert successor.root_task_id == task_id and successor.payload == payload
                successor_id = successor.id
            assert not VerificationService(settings, connections).publish(
                task_id, old_token, payload, {}
            )
        finally:
            if held is not None:
                held.rollback()
                held.close()
            docker("up", "-d", "--wait", "--wait-timeout", "120", "worker")
        wait_ready()
        result = wait_task(api, successor_id, {"succeeded"}, timeout=90)
        assert result["semantic_asset_id"] == identity
        assert result["result"]["origin_artifact"] == payload["origin_artifact"]
        assert result["result"]["consumer_hash"] == payload["consumer_hash"]
        assert result["result"]["shape"] == [660, 3076]
        assert result["result"]["capabilities"] == catalog.CAPABILITIES
        with Session(connections.engine) as db:
            assert db.get(AsyncTask, successor_id).execution_token != old_token
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(OperationLog)
                    .where(
                        OperationLog.object_id == successor_id,
                        OperationLog.action == "semantic.verify.completed",
                    )
                )
                == 1
            )
        assert before == {p.name: p.read_bytes() for p in (catalog.STORE / identity).iterdir()}
