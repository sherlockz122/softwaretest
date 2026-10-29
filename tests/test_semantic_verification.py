"""Real MySQL/outbox/fence tests; synthetic assets do not certify model quality."""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session

from apps.api.application import create_app
from packages.auth.security import PASSWORD_HASHER, AuthError, CurrentUser
from packages.mining import public_assets as assets
from packages.persistence.models import AsyncTask, OperationLog, TaskOutbox, User
from packages.platform import acceptance_evidence as evidence
from packages.platform.connections import Connections
from packages.semantic import asset_catalog as catalog
from packages.semantic import verification as module
from packages.semantic.asset_reader import ConsumptionError
from packages.tasks.service import TaskError
from scripts import collect_fresh_task_evidence as evidence_driver
from tests.auth_support import migrate, scratch_database
from tests.semantic_asset_support import source

pytestmark = pytest.mark.integration
PASSWORD = "semantic-task-test-password"


@pytest.fixture(scope="module")
def database():
    with scratch_database() as (settings, engine):
        migrate(engine)
        actors = []
        with Session(engine) as db, db.begin():
            for role in ("Viewer", "Member", "Admin", "Member"):
                user = User(
                    id=str(uuid4()),
                    username="sv-" + uuid4().hex[:12],
                    role=role,
                    password_hash=PASSWORD_HASHER.hash(PASSWORD),
                    is_active=True,
                )
                db.add(user)
                actors.append(CurrentUser(user.id, user.username, role))
        yield settings, engine, actors


@pytest.fixture
def environment(database, monkeypatch):
    settings, engine, actors = database
    parent = assets.ROOT / "runtime/temp"
    parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=parent, prefix="semantic-task-") as temp:
        directory, manifest = source(Path(temp))
        store = Path(temp) / "installed"
        installed = catalog.install(directory, manifest["content_hash"], store=store)
        monkeypatch.setattr(catalog, "STORE", store)
        connections = Connections(settings)
        try:
            service = module.VerificationService(settings, connections, store=store)
            yield service, installed, actors, settings, engine
        finally:
            connections.close()


def create(environment, key=None):
    service, installed, actors, *_ = environment
    return service.create(actors[1], key or uuid4().hex, installed.name, str(uuid4()))["task_id"]


def claim(environment):
    task_id = create(environment)
    token, payload = environment[0].tasks.claim(task_id)
    return task_id, token, payload


def test_concurrent_replay_is_one_persisted_request_and_outbox(environment):
    service, installed, actors, _, engine = environment
    key = uuid4().hex
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(environment, key), range(2)))
    assert results[0] == results[1]
    with pytest.raises(ConsumptionError, match="TASK_IDEMPOTENCY_CONFLICT"):
        service.create(actors[1], key, "0" * 64, str(uuid4()))
    with Session(engine) as db:
        task = db.get(AsyncTask, results[0])
        assert task.type == module.KIND and task.root_task_id == task.id
        assert task.payload["asset_id"] == installed.name
        assert task.payload["consumer_hash"] == module.consumer_hash()
        assert (
            db.scalar(
                select(func.count()).select_from(TaskOutbox).where(TaskOutbox.task_id == task.id)
            )
            == 1
        )


def test_complete_snapshot_hashes_capabilities_and_audit_atomic(environment):
    service, installed, _, _, engine = environment
    before = {p.name: p.read_bytes() for p in installed.iterdir()}
    task_id, token, payload = claim(environment)
    service.run(task_id, token, payload)
    view = service.tasks.detail(task_id)
    assert view["status"] == "succeeded" and view["processed"] == view["total"] == 660
    assert view["semantic_asset_id"] == installed.name
    result = view["result"]
    assert result["shape"] == [660, 3076] and result["partitions"] == {
        "train": 488,
        "validation": 172,
    }
    assert result["snapshot_only"] and result["verified_at"].endswith("Z")
    assert result["capabilities"] == catalog.CAPABILITIES
    assert (
        not result["capabilities"]["training_ready"]
        and not result["capabilities"]["candidate_release"]
    )
    assert result["payload_hashes"] == catalog.verify(installed)[0]["files"]
    assert before == {p.name: p.read_bytes() for p in installed.iterdir()}
    assert not any(
        word in str(result)
        for word in ["runtime/", "password", "records", "text_hash", "dependencies"]
    )
    with Session(engine) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(OperationLog)
                .where(
                    OperationLog.object_id == task_id,
                    OperationLog.action == "semantic.verify.completed",
                )
            )
            == 1
        )
    assert service.tasks.listing(module.KIND, "succeeded")["total"] >= 1


def test_real_database_child_collector_preserves_new_kind_and_only_safe_fields(
    environment, monkeypatch
):
    service, installed, _, settings, _ = environment
    task_id, token, payload = claim(environment)
    service.run(task_id, token, payload)
    monkeypatch.setenv("DG_MYSQL_DATABASE", settings.mysql_database)
    code = (
        "import json; from packages.platform.acceptance_evidence import collect_database; "
        "print(json.dumps(collect_database(),ensure_ascii=True))"
    )
    target = installed.parent.parent / "collected-tasks.json"
    assert evidence_driver.capture([sys.executable, "-c", code], target)
    saved = json.loads(target.read_text())
    row = next(row for row in saved["tasks"] if row["id"] == task_id)
    assert row == {
        "id": task_id,
        "root_task_id": task_id,
        "type": "semantic.verify",
        "status": "succeeded",
        "error_code": None,
    }
    assert set(row) == evidence.FIELDS
    assert saved["collection_status"] == "succeeded" and not saved["truncated"]
    assert "consumer_hash" not in target.read_text() and "origin_artifact" not in target.read_text()


@pytest.mark.parametrize(
    "field,value",
    [
        ("semantic_verification_version", "old"),
        ("rule_hash", "0" * 64),
        ("consumer_hash", "0" * 64),
        ("origin_artifact", "0" * 64),
    ],
)
def test_old_or_drifting_binding_never_publishes_success(environment, field, value):
    service, _, _, _, engine = environment
    task_id = create(environment)
    with Session(engine) as db, db.begin():
        row = db.get(AsyncTask, task_id)
        row.payload = row.payload | {field: value}
    token, payload = service.tasks.claim(task_id)
    service.run(task_id, token, payload)
    view = service.tasks.detail(task_id)
    assert view["status"] == "failed" and view["result"] is None
    assert view["error"]["code"] in {"SEMANTIC_TASK_BINDING_CONFLICT", "SEMANTIC_ASSET_DRIFT"}


def test_executable_change_after_queue_rejected(environment, monkeypatch):
    task_id, token, payload = claim(environment)
    monkeypatch.setattr(module, "consumer_hash", lambda: "0" * 64)
    environment[0].run(task_id, token, payload)
    view = environment[0].tasks.detail(task_id)
    assert view["status"] == "failed" and view["error"]["code"] == "SEMANTIC_TASK_BINDING_CONFLICT"
    assert view["result"] is None


def test_cancellation_during_actual_reads_wins_without_partial_result(environment):
    service, _, actors, *_ = environment
    task_id, token, payload = claim(environment)
    cancelled = False

    def observe():
        nonlocal cancelled
        if not cancelled:
            cancelled = True
            assert service.tasks.cancel(actors[1], task_id, str(uuid4()))[0] == 202

    service.run(task_id, token, payload, observe=observe)
    view = service.tasks.detail(task_id)
    assert cancelled and view["status"] == "cancelled" and view["result"] is None


def test_expired_lease_successor_preserves_input_and_rejects_old_token(environment):
    service, _, _, _, engine = environment
    task_id, token, payload = claim(environment)
    with engine.begin() as db:
        db.execute(
            text(
                "UPDATE async_task SET lease_until=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE id=:id"
            ),
            {"id": task_id},
        )
    assert not service.heartbeat(task_id, token, payload)
    service.tasks.reconcile()
    with Session(engine) as db:
        successor = db.scalar(select(AsyncTask).where(AsyncTask.retry_of == task_id))
        assert (
            successor is not None
            and successor.payload == payload
            and successor.root_task_id == task_id
        )
        successor_id = successor.id
    assert not service.publish(task_id, token, payload, {})
    new_token, new_payload = service.tasks.claim(successor_id)
    service.run(successor_id, new_token, new_payload)
    assert service.tasks.detail(task_id)["status"] == "failed"
    assert service.tasks.detail(task_id)["result"] is None
    assert service.tasks.detail(successor_id)["status"] == "succeeded"


def test_same_task_wrong_token_and_changed_stored_payload_rejected(environment):
    service, _, _, _, engine = environment
    task_id, token, payload = claim(environment)
    assert not service.publish(task_id, str(uuid4()), payload, {})
    with Session(engine) as db, db.begin():
        row = db.get(AsyncTask, task_id)
        row.payload = row.payload | {"origin_artifact": "f" * 64}
    service.run(task_id, token, payload)
    assert service.tasks.detail(task_id)["result"] is None
    assert service.tasks.detail(task_id)["status"] == "failed"


def test_payload_corruption_after_full_verification_rejected_before_commit(environment):
    service, installed, _, _, _ = environment
    task_id, token, payload = claim(environment)
    value = catalog.verify(installed)[0]
    path = installed / "embeddings.npy"
    with path.open("r+b") as stream:
        stream.seek(-4, 2)
        stream.write(b"\0\0\0\0")
    with pytest.raises(ConsumptionError, match="SEMANTIC_ASSET_DRIFT"):
        service.publish(task_id, token, payload, value)
    assert service.tasks.detail(task_id)["result"] is None
    assert service.tasks.detail(task_id)["status"] == "running"


def test_publication_failure_rolls_back_result_and_success_audit(environment):
    service, installed, _, _, engine = environment
    task_id, token, payload = claim(environment)
    value = catalog.verify(installed)[0]

    def fail_flush(db, *_):
        if any(
            isinstance(row, AsyncTask) and row.id == task_id and row.result_json for row in db.dirty
        ):
            raise RuntimeError("injected verification commit failure")

    event.listen(Session, "before_flush", fail_flush)
    try:
        with pytest.raises(RuntimeError, match="injected verification commit"):
            service.publish(task_id, token, payload, value)
    finally:
        event.remove(Session, "before_flush", fail_flush)
    view = service.tasks.detail(task_id)
    assert view["status"] == "running" and view["result"] is None
    with Session(engine) as db:
        assert not db.scalar(
            select(OperationLog.id).where(
                OperationLog.object_id == task_id,
                OperationLog.action == "semantic.verify.completed",
            )
        )
    assert service.publish(task_id, token, payload, value)


def test_owner_admin_retry_and_viewer_or_other_member_cannot_control(environment):
    service, _, actors, *_ = environment
    task_id = create(environment)
    for actor in [actors[0], actors[3]]:
        with pytest.raises(AuthError):
            service.tasks.cancel(actor, task_id, str(uuid4()))
    assert service.tasks.cancel(actors[2], task_id, str(uuid4()))[0] == 204
    retry = service.tasks.retry(actors[1], task_id, "retry", str(uuid4()))
    assert retry == service.tasks.retry(actors[1], task_id, "retry", str(uuid4()))
    assert retry["retry_of"] == task_id
    with pytest.raises(TaskError, match="TASK_RETRY_EXISTS"):
        service.tasks.retry(actors[2], task_id, "different", str(uuid4()))
    token, payload = service.tasks.claim(retry["task_id"])
    service.run(retry["task_id"], token, payload)
    assert service.tasks.detail(retry["task_id"])["status"] == "succeeded"


def login(api, actor):
    response = api.post(
        "/api/v1/auth/login", json={"username": actor.username, "password": PASSWORD}
    )
    assert response.status_code == 201
    return {
        "Authorization": "Bearer " + response.json()["access_token"],
        "Idempotency-Key": uuid4().hex,
    }


def test_authenticated_api_replay_no_body_and_task_filter(environment):
    service, installed, actors, settings, _ = environment
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as api:
        path = f"/api/v1/semantic-assets/{installed.name}/verifications"
        assert api.post(path, headers={"Idempotency-Key": "anonymous"}).status_code == 401
        assert api.post(path, headers=login(api, actors[0])).status_code == 403
        headers = login(api, actors[1])
        for body in [{}, {"path": "C:/arbitrary"}, {"training_ready": True}]:
            response = api.post(path, headers=headers, json=body)
            assert (
                response.status_code == 422
                and response.json()["code"] == "SEMANTIC_TASK_INVALID_INPUT"
            )
        first = api.post(path, headers=headers)
        assert first.status_code == 202 and first.headers["Cache-Control"] == "no-store"
        assert api.post(path, headers=headers).json() == first.json()
        task_id = first.json()["task_id"]
        token, payload = service.tasks.claim(task_id)
        service.run(task_id, token, payload)
        response = api.get(f"/api/v1/tasks/{task_id}", headers=headers)
        assert (
            response.status_code == 200 and response.json()["semantic_asset_id"] == installed.name
        )
        response = api.get("/api/v1/tasks?type=semantic.verify&status=succeeded", headers=headers)
        assert response.status_code == 200 and response.json()["total"] >= 1
        assert all(item["type"] == "semantic.verify" for item in response.json()["items"])
        assert api.post(path, headers=login(api, actors[2])).status_code == 202


def test_current_disabled_and_viewer_authorization_precedes_file_observation(
    environment, monkeypatch
):
    service, installed, actors, settings, engine = environment

    def unexpected(*_):
        pytest.fail("unauthorized filesystem observation")

    monkeypatch.setattr(catalog, "receipt", unexpected)
    with pytest.raises(AuthError):
        service.create(actors[0], "viewer", installed.name, str(uuid4()))
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as api:
        headers = login(api, actors[1])
        with Session(engine) as db, db.begin():
            db.get(User, actors[1].id).is_active = False
        try:
            response = api.post(
                f"/api/v1/semantic-assets/{installed.name}/verifications", headers=headers
            )
            assert response.status_code == 401
        finally:
            with Session(engine) as db, db.begin():
                db.get(User, actors[1].id).is_active = True
