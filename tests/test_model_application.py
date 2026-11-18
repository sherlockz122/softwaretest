"""Real isolated MySQL, supervised CPU jobs, RBAC and artifact fences."""

import json
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, inspect, select, text
from sqlalchemy.orm import Session

from apps.api.application import create_app
from packages.auth.security import PASSWORD_HASHER, CurrentUser
from packages.mining import public_assets as assets
from packages.mining import public_dataset
from packages.mining import research_dataset as research
from packages.mining.dataset_service import DatasetService
from packages.modeling import policy
from packages.modeling.policy import ModelError
from packages.modeling.service import ModelService
from packages.persistence.models import AsyncTask, DatasetMember, ModelArtifact, ModelRun, User
from packages.platform.connections import Connections
from packages.tasks.service import TaskError, database_now
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


def test_training_without_validation_partition(environment):
    service, _, users, settings, engine, dataset = environment
    source, imported = fixture_asset(
        research.STORE.parent / "two-way-source", rows=80, store=research.STORE
    )
    body = {
        "name": "two-way model fixture",
        "asset_id": source.name,
        "source_hash": imported["manifest"]["content_hash"],
        "purpose": research.PURPOSE,
        "split_strategy": "time_ordered",
        "train_ratio": 0.75,
        "validation_ratio": 0.0,
        "test_ratio": 0.25,
    }
    ack = dataset.create(users[1], uuid4().hex, body, str(uuid4()))
    token, payload = dataset.tasks.claim(ack["task_id"])
    dataset.run(ack["task_id"], token, payload)
    detail = dataset.detail(ack["dataset_id"])
    assert detail["ready"]
    assert set(detail["summary"]["partitions"]) == {"train", "test"}
    train_ack, train_token, train_payload = claim(
        environment,
        {"dataset_id": ack["dataset_id"], "plugin": "lr", "params": {}, "seed": 42},
    )
    service.run(train_ack["task_id"], train_token, train_payload)
    result = service.detail(train_ack["model_run_id"])
    assert result["registered"], service.tasks.detail(train_ack["task_id"])
    report = result["report"]
    assert report["calibration_partition"] == "train"
    assert report["calibration"]["method"] == "train-chronological-sigmoid-logit-v1"
    assert "train_threshold" in report["metrics"]
    assert "validation_threshold" not in report["metrics"]
    assert report["metrics"]["test"]["support"] == {"clean": 10, "buggy": 10}
    assert "no_validation_partition_train_internal_calibration" in report["limitations"]
    assert service.risk_items(train_ack["model_run_id"])["partitions"] == ["test"]


def test_imported_csv_full_chain_build_download_train(database, monkeypatch):
    """Import a small CSV through the API, build a two-partition dataset on
    real MySQL, download its CSV artifacts and train a model on it."""
    settings, engine, users = database
    parent = assets.ROOT / "runtime/temp"
    parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=parent, prefix="import-chain-") as name:
        root = Path(name)
        monkeypatch.setattr(research, "STORE", root / "assets")
        monkeypatch.setattr(research, "OUTPUT", root / "datasets")
        monkeypatch.setattr(policy, "OUTPUT", root / "models")
        connections = Connections(settings)
        try:
            numeric = list(public_dataset.NUMERIC_FIELDS)
            header = ["repo", "sha", "time", "bug", *numeric, "fix"]
            lines = [",".join(header)]
            for i in range(60):
                # 标签 0/1 交替；事件时间各不相同（i*10），保证 time_ordered
                # 切出的测试分区同时包含两类。
                lines.append(
                    ",".join(
                        [
                            "user/imported",
                            f"import-{i:04d}",
                            str(i * 10),
                            "1" if i % 2 else "0",
                            *[str((i % 7) + 1) for _ in numeric],
                            "True" if i % 4 == 0 else "False",
                        ]
                    )
                )
            payload = ("\n".join(lines) + "\n").encode("utf-8")
            mapping = {
                "canonical_repository": "repo",
                "commit_id": "sha",
                "event_epoch": "time",
                "label": "bug",
                **{field: field for field in numeric},
                "fix": "fix",
            }
            with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
                def login(index):
                    response = client.post(
                        "/api/v1/auth/login",
                        json={
                            "username": users[index].username,
                            "password": "model-test-only-password",
                        },
                    )
                    assert response.status_code == 201
                    return {
                        "Authorization": "Bearer " + response.json()["access_token"]
                    }

                params = {"columns": json.dumps(mapping), "license": "CC0-1.0"}
                assert (
                    client.post(
                        "/api/v1/assets/import",
                        params=params,
                        headers=login(0),
                        content=payload,
                    ).status_code
                    == 403
                )
                response = client.post(
                    "/api/v1/assets/import",
                    params=params,
                    headers=login(1),
                    content=payload,
                )
                assert response.status_code == 201
                asset = response.json()
                assert asset["rows"] == 60 and asset["source_kind"] == "user_import"
                assert asset["acquired_at"] is None
                # 缺数值特征列 → 422 并列出缺口
                bad_columns = json.dumps(
                    {
                        "canonical_repository": "repo",
                        "commit_id": "sha",
                        "event_epoch": "time",
                        "label": "bug",
                        "fix": "fix",
                    }
                )
                bad = client.post(
                    "/api/v1/assets/import",
                    params={"columns": bad_columns, "license": "CC0-1.0"},
                    headers=login(1),
                    content=payload,
                )
                assert bad.status_code == 422
                assert bad.json()["code"] == "DATASET_IMPORT_COLUMNS_INVALID"
                assert "la" in bad.json()["message"]
                # Admin 路径通道：先读列再导入；同内容同映射同许可 → 同一资产
                local = root / "server-local.csv"
                local.write_text("\n".join(lines), encoding="utf-8")
                assert (
                    client.post(
                        "/api/v1/assets/import-from-path",
                        headers=login(1),
                        json={
                            "path": str(local),
                            "columns": mapping,
                            "license": "CC0-1.0",
                        },
                    ).status_code
                    == 403
                )
                admin = login(2)
                listed = client.get(
                    "/api/v1/assets/import-columns",
                    params={"path": str(local)},
                    headers=admin,
                )
                assert listed.status_code == 200
                assert listed.json()["columns"] == header
                pathed = client.post(
                    "/api/v1/assets/import-from-path",
                    headers=admin,
                    json={
                        "path": str(local),
                        "columns": mapping,
                        "license": "CC0-1.0",
                    },
                )
                assert pathed.status_code == 201
                assert pathed.json()["asset_id"] == asset["asset_id"]

            dataset = DatasetService(settings, connections)
            body = {
                "name": "imported chain",
                "asset_id": asset["asset_id"],
                "source_hash": asset["source_hash"],
                "purpose": research.PURPOSE,
                "split_strategy": "time_ordered",
                "train_ratio": 0.75,
                "validation_ratio": 0.0,
                "test_ratio": 0.25,
            }
            ack = dataset.create(users[1], uuid4().hex, body, str(uuid4()))
            token, run_payload = dataset.tasks.claim(ack["task_id"])
            dataset.run(ack["task_id"], token, run_payload)
            detail = dataset.detail(ack["dataset_id"])
            assert detail["ready"] and detail["total"] == 60
            assert set(detail["summary"]["partitions"]) == {"train", "test"}
            acquisition = detail["summary"]["source_acquisition"]
            assert acquisition["source_kind"] == "user_import"
            assert acquisition["import_license_declaration"] == "CC0-1.0"
            assert acquisition["import_columns"] == mapping
            assert acquisition["fix_column_mapped"] is True
            assert detail["summary"]["target"] == "user_declared_import_label"
            assert detail["summary"]["event_clock"] == "user_declared_event_epoch_UTC"
            assert detail["summary"]["auxiliary_features"] == ["fix", "lt"]
            member = detail["members"][0]
            assert member["fix"] in (True, False)
            assert member["fix_missing_reason"] is None
            assert member["lt"] is None
            with pytest.raises(TaskError, match="DATA_PARTITION_ABSENT"):
                dataset.download(ack["dataset_id"], "validation")
            csv = dataset.download(ack["dataset_id"], "train").read_text()
            rows = csv.splitlines()
            assert rows[0].endswith(",fix,lt")
            assert len(rows) == 46
            for line in rows[1:]:
                cells = line.split(",")
                assert cells[-2] in ("True", "False") and cells[-1] == ""

            service = ModelService(settings, connections)
            request = {
                "dataset_id": ack["dataset_id"],
                "plugin": "lr",
                "params": {},
                "seed": 42,
            }
            model_ack = service.create(users[1], uuid4().hex, request, str(uuid4()))
            model_token, model_payload = service.tasks.claim(model_ack["task_id"])
            service.run(model_ack["task_id"], model_token, model_payload)
            result = service.detail(model_ack["model_run_id"])
            assert result["registered"], service.tasks.detail(model_ack["task_id"])
            report = result["report"]
            assert report["calibration_partition"] == "train"
            assert report["metrics"]["test"]["support"] == {"clean": 7, "buggy": 8}
            assert service.download(model_ack["model_run_id"], "report.json").is_file()
        finally:
            connections.close()


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


def test_feature_choice_contract_and_replay(environment):
    service, body, users, *_ = environment
    key = uuid4().hex
    body = body | {
        "features": ["age", "la", "ld"],
        "forms": {"la": "log1p", "ld": "standardize"},
    }
    ack = service.create(users[1], key, body, str(uuid4()))
    import json

    # 重放与冲突必须在 claim 前断言：claim 会改任务状态，ack 里的 status 就变了
    assert service.create(users[1], key, body, str(uuid4())) == ack
    with pytest.raises(ModelError, match="TASK_IDEMPOTENCY_CONFLICT"):
        service.create(
            users[1], key, body | {"features": ["la"], "forms": {"la": "log1p"}}, str(uuid4())
        )
    token, payload = service.tasks.claim(ack["task_id"])
    request = json.loads(payload["request_json"])
    # 归一化：顺序固定为数据集字段顺序，缺省形式按插件补全
    assert request["features"] == ["la", "ld", "age"]
    assert request["forms"] == {"la": "log1p", "ld": "standardize", "age": "standardize"}
    service.run(ack["task_id"], token, payload)
    result = service.detail(ack["model_run_id"])
    assert result["registered"], service.tasks.detail(ack["task_id"])
    report = result["report"]
    assert report["features"] == ["la", "ld", "age"]
    assert report["forms"] == {"la": "log1p", "ld": "standardize", "age": "standardize"}
    assert set(report["standardization"]) == {"ld", "age"}
    assert report["request"]["features"] == report["features"]
    assert report["metrics"]["test"]["support"] == {"clean": 6, "buggy": 6}


def test_feature_choice_rejected_without_task(environment):
    service, body, users, _, engine, _ = environment
    # 共享库可能已有其他用例的模型行：比较前后计数，而不是断言全局为 0
    with Session(engine) as db:
        before = db.scalar(select(func.count()).select_from(ModelRun))
    for bad in (
        {"features": []},
        {"features": ["la", "la"]},
        {"features": ["bogus"]},
        {"features": "la"},
        {"forms": {"bogus": "raw"}},
        {"forms": {"la": "square"}},
        {"forms": []},
    ):
        with pytest.raises(ModelError, match="MODEL_FEATURE_INVALID"):
            service.create(users[1], uuid4().hex, body | bad, str(uuid4()))
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(ModelRun)) == before


def test_pool_add_and_reuse_full_chain(environment):
    service, body, users, settings, engine, dataset = environment
    import numpy as np
    import skops.io as sio

    from packages.auth.security import AuthError
    from packages.modeling import pool
    from packages.persistence.models import ModelPoolEntry

    # 1. 从零训练一份 LR 运行并加入池
    ack, token, payload = claim(environment)
    service.run(ack["task_id"], token, payload)
    source_run = service.detail(ack["model_run_id"])
    assert source_run["registered"]
    with pytest.raises(AuthError, match="AUTH_FORBIDDEN"):
        service.pool_add(users[0], ack["model_run_id"])
    entry = service.pool_add(users[1], ack["model_run_id"])
    entry_id = entry["id"]
    assert entry["source_kind"] == "run" and entry["owner_id"] == users[1].id
    assert service.pool_add(users[1], ack["model_run_id"])["id"] == entry_id
    assert any(item["id"] == entry_id for item in service.pool_list()["items"])

    # 2. 同一存储里再建一份数据集，供复用训练使用
    with TemporaryDirectory(dir=assets.ROOT / "runtime/temp", prefix="pool-second-") as name:
        _, second_source = fixture_asset(
            Path(name),
            rows=100,
            projects=("apache/commons-io",),
            store=research.STORE,
        )
        asset_b = second_source["manifest"]["asset_id"]
        hash_b = second_source["manifest"]["content_hash"]
    body_b = {
        "name": "pool reuse fixture",
        "asset_id": asset_b,
        "source_hash": hash_b,
        "purpose": research.PURPOSE,
        "split_strategy": "time_ordered",
        "train_ratio": 0.7,
        "validation_ratio": 0.15,
        "test_ratio": 0.15,
    }
    ack_b = dataset.create(users[1], uuid4().hex, body_b, str(uuid4()))
    token_b, payload_b = dataset.tasks.claim(ack_b["task_id"])
    dataset.run(ack_b["task_id"], token_b, payload_b)
    assert dataset.detail(ack_b["dataset_id"])["ready"]

    # 3. 显式复用：估计器参数一致，校准在新数据集重做，报告标注来源
    reuse_body = {
        "dataset_id": ack_b["dataset_id"],
        "plugin": "lr",
        "params": {},
        "seed": 7,
        "reuse_from": entry_id,
    }
    ack2 = service.create(users[1], uuid4().hex, reuse_body, str(uuid4()))
    token2, payload2 = service.tasks.claim(ack2["task_id"])
    service.run(ack2["task_id"], token2, payload2)
    reused = service.detail(ack2["model_run_id"])
    assert reused["registered"]
    report = reused["report"]
    assert report["reused_from"]["pool_entry_id"] == entry_id
    assert report["reused_from"]["source_kind"] == "run"
    assert (
        "estimator_parameters_reused_from_pool_calibration_threshold_refit"
        in report["limitations"]
    )
    path_a, _, _ = service.artifact(ack["model_run_id"], "model.skops")
    path_b, _, _ = service.artifact(ack2["model_run_id"], "model.skops")
    bundle_a = sio.load(path_a, trusted=[])
    bundle_b = sio.load(path_b, trusted=[])
    assert np.array_equal(bundle_a["model"].coef_, bundle_b["model"].coef_)
    assert report["calibration"]["calibration_rows"] != source_run["report"]["calibration"][
        "calibration_rows"
    ]
    risk = service.risk_items(ack2["model_run_id"], 1, 10)
    assert set(risk) >= {"items", "total", "threshold"}

    # 4. 显式指定契约不一致的池条目被拒绝（create 会弹出 reuse_from，用显式副本）
    with pytest.raises(ModelError, match="MODEL_POOL_CONTRACT_MISMATCH"):
        service.create(
            users[1],
            uuid4().hex,
            dict(reuse_body, params={"c": 2.0}, reuse_from=entry_id),
            str(uuid4()),
        )
    with pytest.raises(ModelError, match="MODEL_POOL_NOT_FOUND"):
        service.create(
            users[1],
            uuid4().hex,
            dict(reuse_body, reuse_from=str(uuid4())),
            str(uuid4()),
        )

    # 5. 自动匹配：零命中回退从零训练并如实标注
    rf_auto = {
        "dataset_id": ack_b["dataset_id"],
        "plugin": "rf",
        "params": {},
        "seed": 7,
        "reuse_policy": "auto",
    }
    ack3 = service.create(users[1], uuid4().hex, rf_auto, str(uuid4()))
    token3, payload3 = service.tasks.claim(ack3["task_id"])
    service.run(ack3["task_id"], token3, payload3)
    report3 = service.detail(ack3["model_run_id"])["report"]
    assert "reused_from" not in report3
    assert "pool_auto_match_none_trained_from_scratch" in report3["limitations"]

    # 6. 自动匹配：命中池条目直接复用
    lr_auto = {
        "dataset_id": ack_b["dataset_id"],
        "plugin": "lr",
        "params": {},
        "seed": 9,
        "reuse_policy": "auto",
    }
    ack4 = service.create(users[1], uuid4().hex, lr_auto, str(uuid4()))
    token4, payload4 = service.tasks.claim(ack4["task_id"])
    service.run(ack4["task_id"], token4, payload4)
    report4 = service.detail(ack4["model_run_id"])["report"]
    assert report4["reused_from"]["pool_entry_id"] == entry_id

    # 7. 移除：非本人 Member 被拒，Admin 可移除；移除后不再可复用
    with pytest.raises(ModelError, match="MODEL_POOL_FORBIDDEN"):
        service.pool_remove(users[3], entry_id)
    service.pool_remove(users[2], entry_id)
    with Session(engine) as db:
        assert db.get(ModelPoolEntry, entry_id).status == "removed"
    with pytest.raises(ModelError, match="MODEL_POOL_NOT_FOUND"):
        service.create(
            users[1], uuid4().hex, dict(reuse_body, reuse_from=entry_id), str(uuid4())
        )

    # 8. 池工件目录与数据库行一一对应
    with Session(engine) as db:
        row = db.get(ModelPoolEntry, entry_id)
    assert (pool.POOL_ROOT / row.id / "estimator.skops").is_file()


def test_preset_registration_and_auto_match(environment):
    service, body, users, settings, engine, dataset = environment
    from sqlalchemy import delete

    from packages.modeling import presets
    from packages.persistence.models import ModelPoolEntry

    with Session(engine) as db, db.begin():
        db.execute(delete(ModelPoolEntry))

    # 1. 首次注册两个预置基线；重复调用幂等
    registered = presets.ensure_presets(engine)
    assert set(registered) == {
        f"preset-lr-{policy.VERSION}",
        f"preset-rf-{policy.VERSION}",
    }
    assert presets.ensure_presets(engine) == []
    listing = service.pool_list()
    presets_rows = [e for e in listing["items"] if e["source_kind"] == "preset"]
    assert len(presets_rows) == 2
    assert all(e["owner_id"] is None for e in presets_rows)
    assert {e["plugin"] for e in presets_rows} == {"lr", "rf"}

    # 2. 自动匹配命中预置 LR 条目：报告标注 preset 来源
    auto = {
        "dataset_id": body["dataset_id"],
        "plugin": "lr",
        "params": {},
        "seed": 11,
        "reuse_policy": "auto",
    }
    ack = service.create(users[1], uuid4().hex, auto, str(uuid4()))
    token, payload = service.tasks.claim(ack["task_id"])
    service.run(ack["task_id"], token, payload)
    report = service.detail(ack["model_run_id"])["report"]
    assert report["reused_from"]["source_kind"] == "preset"
    assert "estimator_parameters_reused_from_pool_calibration_threshold_refit" in report[
        "limitations"
    ]

    # 3. 预置条目只有管理员能移除
    preset_id = next(e["id"] for e in presets_rows if e["plugin"] == "lr")
    with pytest.raises(ModelError, match="MODEL_POOL_FORBIDDEN"):
        service.pool_remove(users[1], preset_id)
    service.pool_remove(users[2], preset_id)
    with Session(engine) as db:
        assert db.get(ModelPoolEntry, preset_id).status == "removed"


def test_risk_api_rejects_unregistered_drift_and_invalid_params(environment):
    service, body, users, settings, _, _ = environment
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        login = lambda index: {  # noqa: E731
            "Authorization": "Bearer "
            + client.post(
                "/api/v1/auth/login",
                json={
                    "username": users[index].username,
                    "password": "model-test-only-password",
                },
            ).json()["access_token"]
        }
        viewer = login(0)
        member = login(1)
        ack = client.post(
            "/api/v1/model-runs",
            headers=member | {"Idempotency-Key": uuid4().hex},
            json=body,
        ).json()
        path = f"/api/v1/model-runs/{ack['model_run_id']}/risk-items"

        # 未注册（任务未运行完成）一律拒绝
        assert client.get(path, headers=viewer).status_code == 409
        assert client.get(path, headers=viewer).json()["code"] == "MODEL_NOT_READY"

        token, payload = service.tasks.claim(ack["task_id"])
        service.run(ack["task_id"], token, payload)

        # 正常读取：Viewer 可读，返回排序、分页与门槛字段
        ok = client.get(path, headers=viewer)
        assert ok.status_code == 200
        body_json = ok.json()
        assert set(body_json) >= {"items", "total", "page", "page_size", "threshold"}

        # 参数非法：API 层校验（页码/页大小/分区取值/类型）
        for params in (
            {"page": 0},
            {"page_size": 101},
            {"partition": ["bogus"]},
            {"page": "x"},
        ):
            response = client.get(path, headers=viewer, params=params)
            assert response.status_code == 422, params
            assert response.json()["code"] == "SYSTEM_INVALID_INPUT", params
        # 服务层校验：同一分区重复传入
        repeated = client.get(path, headers=viewer, params={"partition": ["train", "train"]})
        assert repeated.status_code == 422
        assert repeated.json()["code"] == "MODEL_INVALID_INPUT"

        # 工件漂移：篡改 probabilities.csv 后拒绝读取
        artifact_path, _record, _report = service.artifact(
            ack["model_run_id"], "probabilities.csv"
        )
        artifact_path.write_bytes(artifact_path.read_bytes() + b"\nbroken")
        drifted = client.get(path, headers=viewer)
        assert drifted.status_code == 409
        assert drifted.json()["code"] == "MODEL_ARTIFACT_DRIFT"
