"""Real isolated MySQL, supervised CPU jobs, RBAC and artifact fences."""

from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, inspect, select, text
from sqlalchemy.orm import Session

from apps.api.application import create_app
from packages.auth.security import PASSWORD_HASHER, CurrentUser
from packages.mining import public_assets as assets
from packages.mining import research_dataset as research
from packages.mining.dataset_service import DatasetService
from packages.modeling import policy
from packages.modeling.policy import ModelError
from packages.modeling.service import ModelService
from packages.persistence.models import AsyncTask, DatasetMember, ModelArtifact, User
from packages.platform.connections import Connections
from packages.tasks.service import database_now
from tests.auth_support import migrate, scratch_database
from tests.dataset_support import fixture_asset


@pytest.fixture(scope="module")
def database():
    with scratch_database() as (settings, engine):
        migrate(engine)
        with Session(engine) as db, db.begin():
            users = []
            for role in ("Viewer", "Member", "Admin", "Member"):
                username = "model-" + uuid4().hex[:12]
                row = User(
                    id=str(uuid4()),
                    username=username,
                    role=role,
                    is_active=True,
                    password_hash=PASSWORD_HASHER.hash("model-test-only-password"),
                )
                db.add(row)
                users.append(CurrentUser(row.id, username, role))
        yield settings, engine, users


@pytest.fixture
def environment(database, monkeypatch):
    settings, engine, users = database
    parent = assets.ROOT / "runtime/temp"
    parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=parent, prefix="model-db-") as name:
        root = Path(name)
        source, imported = fixture_asset(root, rows=80)
        monkeypatch.setattr(research, "STORE", source.parent)
        monkeypatch.setattr(research, "OUTPUT", root / "datasets")
        monkeypatch.setattr(policy, "OUTPUT", root / "models")
        connections = Connections(settings)
        dataset = DatasetService(settings, connections)
        body = {
            "name": "model fixture",
            "asset_id": source.name,
            "source_hash": imported["manifest"]["content_hash"],
            "purpose": research.PURPOSE,
            "split_strategy": "time_ordered",
            "train_ratio": 0.7,
            "validation_ratio": 0.15,
            "test_ratio": 0.15,
        }
        ack = dataset.create(users[1], uuid4().hex, body, str(uuid4()))
        token, payload = dataset.tasks.claim(ack["task_id"])
        dataset.run(ack["task_id"], token, payload)
        assert dataset.detail(ack["dataset_id"])["ready"]
        service = ModelService(settings, connections)
        request = {"dataset_id": ack["dataset_id"], "plugin": "lr", "params": {}, "seed": 42}
        try:
            yield service, request, users, settings, engine, dataset
        finally:
            connections.close()


def claim(environment, request=None):
    service, default, users, *_ = environment
    ack = service.create(users[1], uuid4().hex, request or default, str(uuid4()))
    token, payload = service.tasks.claim(ack["task_id"])
    return ack, token, payload


def test_idempotency_unready_and_param_conflicts(environment):
    service, body, users, *_ = environment
    key = uuid4().hex
    first = service.create(users[1], key, body, str(uuid4()))
    assert (
        service.create(users[1], key, body | {"params": {"c": 1.0, "max_iter": 500}}, str(uuid4()))
        == first
    )
    with pytest.raises(ModelError, match="TASK_IDEMPOTENCY_CONFLICT"):
        service.create(users[1], key, body | {"seed": 17}, str(uuid4()))
    with pytest.raises(ModelError, match="MODEL_NOT_READY"):
        service.download(first["model_run_id"], "report.json")
    assert service.detail(first["model_run_id"])["report"] is None


def test_request_float_survives_mysql_claim_replay_and_training(environment):
    service, body, users, *_ = environment
    value = float("1.2068478319973799")
    body = body | {"params": {"c": value}}
    key = uuid4().hex
    ack = service.create(users[1], key, body, str(uuid4()))
    assert service.create(users[1], key, body, str(uuid4())) == ack
    token, payload = service.tasks.claim(ack["task_id"])
    import json

    assert json.loads(payload["request_json"])["params"]["c"].hex() == value.hex()
    service.run(ack["task_id"], token, payload)
    result = service.detail(ack["model_run_id"])
    assert result["registered"], service.tasks.detail(ack["task_id"])
    assert result["params"]["c"].hex() == value.hex()
    assert result["report"]["request"]["params"]["c"].hex() == value.hex()


@pytest.mark.parametrize("plugin", ["lr", "rf"])
def test_real_training_roundtrip_and_registration(environment, plugin):
    service, request, *_ = environment
    ack, token, payload = claim(environment, request | {"plugin": plugin})
    service.run(ack["task_id"], token, payload)
    result = service.detail(ack["model_run_id"])
    assert result["registered"], service.tasks.detail(ack["task_id"])
    assert result["report"]["metrics"]["test"]["support"] == {"clean": 6, "buggy": 6}
    assert result["report"]["calibration"]["calibration_rows"] == 6
    assert not result["candidate_release"]
    for name in policy.FILES:
        assert service.download(ack["model_run_id"], name).is_file()
    assert service.tasks.claim(ack["task_id"]) is None


def test_supervisor_cancels_child_and_does_not_register(environment):
    service, _, users, *_ = environment
    ack, token, payload = claim(environment)
    ids = []

    def cancel(process, stage):
        if not ids:
            ids.append(process.pid)
            service.tasks.cancel(users[1], ack["task_id"], str(uuid4()))

    service.run(ack["task_id"], token, payload, observe=cancel)
    assert ids
    assert service.tasks.detail(ack["task_id"])["status"] == "cancelled"
    assert not service.detail(ack["model_run_id"])["registered"]
    import psutil

    assert not psutil.pid_exists(ids[0])


def test_subprocess_fault_retry_same_root_and_old_fence(environment):
    service, _, users, _, engine, _ = environment
    ack, token, payload = claim(environment)
    service.run(ack["task_id"], token, payload, observe=lambda p, _: p.kill())
    assert service.tasks.detail(ack["task_id"])["status"] == "failed"
    retry = service.tasks.retry(users[1], ack["task_id"], uuid4().hex, str(uuid4()))
    next_token, next_payload = service.tasks.claim(retry["task_id"])
    assert not service.heartbeat(ack["task_id"], token)
    service.run(retry["task_id"], next_token, next_payload)
    result = service.detail(ack["model_run_id"])
    assert result["registered"] and result["task_id"] == retry["task_id"]
    with Session(engine) as db:
        assert (
            len(
                db.scalars(
                    select(ModelArtifact).where(ModelArtifact.model_run_id == ack["model_run_id"])
                ).all()
            )
            == 3
        )


def test_expired_token_and_auto_retry_cannot_publish(environment):
    service, _, _, _, engine, _ = environment
    ack, token, payload = claim(environment)
    with Session(engine) as db, db.begin():
        db.get(AsyncTask, ack["task_id"]).lease_until = database_now(db) - timedelta(seconds=1)
    service.tasks.reconcile()
    service.run(ack["task_id"], token, payload)
    with Session(engine) as db:
        child = db.scalar(select(AsyncTask).where(AsyncTask.retry_of == ack["task_id"]))
        retry_id = child.id
    next_token, next_payload = service.tasks.claim(retry_id)
    service.run(retry_id, next_token, next_payload)
    assert service.detail(ack["model_run_id"])["registered"]


def test_db_member_drift_and_capacity_refuse(environment, monkeypatch):
    service, body, users, _, engine, _ = environment
    ack, token, payload = claim(environment)
    with Session(engine) as db, db.begin():
        row = db.get(DatasetMember, (body["dataset_id"], 0))
        values = row.numeric_json
        row.numeric_json = values | {"age": values["age"] + 0.0000000000001}
    service.run(ack["task_id"], token, payload)
    assert service.tasks.detail(ack["task_id"])["error"]["code"] == "MODEL_DATA_DRIFT"
    next_ack = service.create(users[1], uuid4().hex, body, str(uuid4()))
    next_token, next_payload = service.tasks.claim(next_ack["task_id"])
    monkeypatch.setattr(
        "packages.modeling.service.shutil.disk_usage", lambda _: SimpleNamespace(free=1024**3)
    )
    service.run(next_ack["task_id"], next_token, next_payload)
    assert service.tasks.detail(next_ack["task_id"])["error"]["code"] == "MODEL_CAPACITY_UNSAFE"


def test_supervisor_memory_limit_terminates_active_child(environment, monkeypatch):
    import psutil

    from packages.modeling import service as module

    service = environment[0]
    ack, token, payload = claim(environment)
    actual = psutil.Process
    children = []

    def excessive(pid=None):
        if pid is None:
            return actual()
        children.append(pid)
        return SimpleNamespace(
            memory_info=lambda: SimpleNamespace(rss=policy.MAX_RSS + 1),
            children=lambda recursive: [],
        )

    monkeypatch.setattr(module.psutil, "Process", excessive)
    service.run(ack["task_id"], token, payload)
    assert children and not psutil.pid_exists(children[0])
    assert service.tasks.detail(ack["task_id"])["error"]["code"] == "MODEL_MEMORY_LIMIT"
    assert not service.detail(ack["model_run_id"])["registered"]


def test_deadline_before_new_work_is_not_registered(environment, monkeypatch):
    service = environment[0]
    ack, token, payload = claim(environment)
    monkeypatch.setattr(policy, "MAX_SECONDS", 0)
    service.run(ack["task_id"], token, payload)
    assert service.tasks.detail(ack["task_id"])["error"]["code"] == "MODEL_DEADLINE"
    assert not service.detail(ack["model_run_id"])["registered"]


def test_atomic_registration_rollback_and_damage_refusal(environment):
    service, _, users, _, engine, _ = environment
    ack, token, payload = claim(environment)

    def fault(session, *_):
        if any(isinstance(r, ModelArtifact) for r in session.new):
            raise RuntimeError("deterministic artifact transaction interruption")

    event.listen(Session, "before_flush", fault)
    try:
        with pytest.raises(RuntimeError):
            service.run(ack["task_id"], token, payload)
    finally:
        event.remove(Session, "before_flush", fault)
    assert not service.detail(ack["model_run_id"])["registered"]
    with Session(engine) as db, db.begin():
        db.get(AsyncTask, ack["task_id"]).lease_until = database_now(db) - timedelta(seconds=1)
    service.tasks.reconcile()
    with Session(engine) as db:
        retry_id = db.scalar(select(AsyncTask.id).where(AsyncTask.retry_of == ack["task_id"]))
    next_token, next_payload = service.tasks.claim(retry_id)
    service.run(retry_id, next_token, next_payload)
    assert service.detail(ack["model_run_id"])["registered"]
    path = service.download(ack["model_run_id"], "report.json")
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ModelError, match="MODEL_ARTIFACT_DRIFT"):
        service.download(ack["model_run_id"], "report.json")
    with pytest.raises(ModelError, match="MODEL_PURPOSE_INCOMPATIBLE"):
        service.release(users[2], ack["model_run_id"])


def test_api_roles_extra_fields_publish_and_listing(environment):
    service, body, users, settings, _, _ = environment
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:

        def login(index):
            response = client.post(
                "/api/v1/auth/login",
                json={"username": users[index].username, "password": "model-test-only-password"},
            )
            assert response.status_code == 201
            return {
                "Authorization": "Bearer " + response.json()["access_token"],
                "Idempotency-Key": uuid4().hex,
            }

        assert client.get("/api/v1/models").status_code == 401
        viewer = login(0)
        assert client.get("/api/v1/models", headers=viewer).json()["production_models"] == []
        assert client.post("/api/v1/model-runs", headers=viewer, json=body).status_code == 403
        member = login(1)
        assert (
            client.post(
                "/api/v1/model-runs",
                headers=member,
                json=body | {"plugin": "rf", "params": {"n_estimators": 10**1000}},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/model-runs", headers=member, json=body | {"path": "C:/outside"}
            ).status_code
            == 422
        )
        ack = client.post("/api/v1/model-runs", headers=member, json=body).json()
        other = login(3)
        assert (
            client.post(f"/api/v1/tasks/{ack['task_id']}/cancel", headers=other).status_code == 403
        )
        token, payload = service.tasks.claim(ack["task_id"])
        service.run(ack["task_id"], token, payload)
        path = f"/api/v1/model-runs/{ack['model_run_id']}"
        assert client.get(path, headers=viewer).json()["registered"]
        assert client.get(path + "?artifact=report.json", headers=viewer).status_code == 200
        assert client.post(path + "/publish", headers=member).status_code == 403
        assert client.post(path + "/publish", headers=login(2)).status_code == 409


def test_entire_downgrade_preflight_preserves_old_tables():
    with scratch_database() as (_, engine):
        migrate(engine)
        migrate(engine, "check")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO user (id,username,password_hash,role,is_active) "
                    "VALUES (:id,:name,'test','Admin',1)"
                ),
                {"id": str(uuid4()), "name": uuid4().hex},
            )
        with pytest.raises(RuntimeError, match="Downgrade refused"):
            migrate(engine, "downgrade", "base")
        assert "model_run" in inspect(engine).get_table_names()
        migrate(engine, "downgrade", "0010_dataset_numbers")
        assert "model_run" not in inspect(engine).get_table_names()
        migrate(engine)


def test_backup_restore_keeps_ids_times_and_artifacts_without_retraining(environment):
    import os
    import re
    import shutil
    import subprocess

    service, _, _, settings, _, _ = environment
    ack, token, payload = claim(environment)
    service.run(ack["task_id"], token, payload)
    original = service.detail(ack["model_run_id"])
    assert original["registered"]
    source = settings.mysql_database
    assert re.fullmatch(r"dg_stage3_test_[0-9a-f]{32}", source)
    docker = os.environ.get("DG_DOCKER_EXE", "docker")
    dump = subprocess.run(
        [
            docker,
            "compose",
            "exec",
            "-T",
            "mysql",
            "sh",
            "-c",
            'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysqldump -uroot '
            '--single-transaction --skip-comments --set-gtid-purged=OFF "$1"',
            "--",
            source,
        ],
        capture_output=True,
        timeout=30,
    )
    assert dump.returncode == 0
    backup = service.output.parent / "model-backup"
    backup.mkdir()
    (backup / "isolated.sql").write_bytes(dump.stdout)
    shutil.copytree(service.output / ack["model_run_id"], backup / ack["model_run_id"])
    with scratch_database() as (restored_settings, engine):
        target = restored_settings.mysql_database
        assert re.fullmatch(r"dg_stage3_test_[0-9a-f]{32}", target)
        restored = subprocess.run(
            [
                docker,
                "compose",
                "exec",
                "-T",
                "mysql",
                "sh",
                "-c",
                'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot "$1"',
                "--",
                target,
            ],
            input=dump.stdout,
            capture_output=True,
            timeout=30,
        )
        assert restored.returncode == 0
        connections = Connections(restored_settings)
        try:
            reader = ModelService(restored_settings, connections, output=backup)
            assert reader.detail(ack["model_run_id"]) == original
            for name in policy.FILES:
                assert (
                    assets.file_digest(reader.download(ack["model_run_id"], name), lambda: None)
                    == original["artifacts"][name]
                )
            with Session(engine) as db:
                assert db.get(AsyncTask, ack["task_id"]).status == "succeeded"
        finally:
            connections.close()
