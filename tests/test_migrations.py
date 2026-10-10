"""INT-MIGRATION：首批表迁移与约束验证（docs/04 第 10.4 节）。

在独立临时数据库上执行 alembic upgrade → 验证表/CHECK/唯一键/索引 →
downgrade 空库回滚 → 再 upgrade，并验证预置账号幂等。
需要运行中的 MySQL（pytest -m integration）。
"""

import os
import secrets
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pymysql
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from packages.domain.models import AsyncTask, TaskOutbox, User
from packages.persistence.db import create_db_engine, create_session_factory
from packages.platform.config import load_settings
from packages.security.passwords import verify_password

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE = "defectguard_migration_test"


def _dotenv_value(name: str) -> str:
    for line in (PROJECT_ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError(f"{name} missing in .env")


def _run(cwd_env, *args):
    result = subprocess.run(
        [sys.executable, "-m", *args],
        cwd=PROJECT_ROOT,
        env=cwd_env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"python -m {' '.join(args)} failed:\n{result.stderr}{result.stdout}"
        )
    return result.stdout


@pytest.fixture(scope="module")
def migrated_db():
    settings = load_settings()
    connection = pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user="root",
        password=_dotenv_value("DG_MYSQL_ROOT_PASSWORD"),
        autocommit=True,
    )
    with connection.cursor() as cursor:
        cursor.execute(f"DROP DATABASE IF EXISTS `{TEST_DATABASE}`")
        cursor.execute(
            f"CREATE DATABASE `{TEST_DATABASE}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        # 迁移与 seed 以应用账号运行，与容器内行为一致；仅授予该测试库。
        cursor.execute(
            f"GRANT ALL PRIVILEGES ON `{TEST_DATABASE}`.* TO `{settings.mysql_user}`@'%'"
        )
    env = {**os.environ, "DG_MYSQL_DATABASE": TEST_DATABASE}
    try:
        _run(env, "alembic", "upgrade", "head")
        yield env
    finally:
        with connection.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{TEST_DATABASE}`")
        connection.close()


def _session_factory():
    return create_session_factory(load_settings(mysql_database=TEST_DATABASE))


def _task(actor_id: str, **overrides):
    now = datetime.now(UTC)
    values = {
        "actor_id": actor_id,
        "type": "scan",
        "scope_key": "repo:demo",
        "idempotency_key": secrets.token_hex(8),
        "payload_hash": "0" * 64,
        "payload": {},
        "status": "queued",
        "progress": 0,
        "processed": 0,
        "total": 10,
        "retry_count": 0,
        "root_task_id": actor_id,
        "queued_deadline": now + timedelta(hours=1),
    }
    values.update(overrides)
    return AsyncTask(**values)


def _outbox(task_id: str, **overrides):
    now = datetime.now(UTC)
    values = {
        "task_id": task_id,
        "event_type": "execute",
        "status": "pending",
        "attempts": 0,
        "next_attempt_at": now,
    }
    values.update(overrides)
    return TaskOutbox(**values)


def _assert_rejected(build, constraint):
    factory = _session_factory()
    with factory() as probe:
        probe.add(build())
        try:
            probe.commit()
        except SQLAlchemyError as exc:
            probe.rollback()
            assert constraint in str(exc), f"expected {constraint} violation, got: {exc}"
        else:
            raise AssertionError(f"expected {constraint} violation")


def test_upgrade_creates_expected_schema(migrated_db):
    engine = create_db_engine(load_settings(mysql_database=TEST_DATABASE))
    try:
        with engine.connect() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = :db"
                    ),
                    {"db": TEST_DATABASE},
                )
            }
            assert {"user", "auth_session", "async_task", "task_outbox", "operation_log"} <= tables

            checks = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT constraint_name FROM information_schema.check_constraints "
                        "WHERE constraint_schema = :db"
                    ),
                    {"db": TEST_DATABASE},
                )
            }
            assert {
                "ck_user_role",
                "ck_async_task_status",
                "ck_async_task_progress",
                "ck_async_task_processed",
                "ck_async_task_total",
                "ck_async_task_retry_count",
                "ck_task_outbox_status",
                "ck_task_outbox_attempts",
            } <= checks

            indexes = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT index_name FROM information_schema.statistics "
                        "WHERE table_schema = :db"
                    ),
                    {"db": TEST_DATABASE},
                )
            }
            for name in (
                "uq_task_idempotency",
                "uq_outbox_task_event",
                "ix_auth_session_user_revoked",
                "ix_auth_session_expires",
                "ix_async_task_status_deadline",
                "ix_async_task_status_lease",
                "ix_async_task_created_id",
                "ix_task_outbox_status_next",
                "ix_task_outbox_status_lease",
                "ix_operation_log_actor_created",
                "ix_operation_log_object",
            ):
                assert name in indexes

            charsets = {
                row[0]: (row[1], row[2])
                for row in connection.execute(
                    text(
                        "SELECT column_name, character_set_name, collation_name "
                        "FROM information_schema.columns "
                        "WHERE table_schema = :db AND table_name = 'user' AND column_name = 'id'"
                    ),
                    {"db": TEST_DATABASE},
                )
            }
            assert charsets["id"] == ("ascii", "ascii_bin")

            version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
            assert version == "20261009_0001"
    finally:
        engine.dispose()


def test_check_constraints_reject_invalid_rows(migrated_db):
    session_factory = _session_factory()
    with session_factory() as session:
        user = User(username="constraint-user", password_hash="x", role="Member")
        session.add(user)
        session.commit()

        try:
            user.role = "Superuser"
            session.commit()
        except SQLAlchemyError as exc:
            session.rollback()
            assert "ck_user_role" in str(exc)
        else:
            raise AssertionError("expected ck_user_role violation")

        # 外键依赖：任务与投递记录各建一条合法基线
        task = _task(user.id)
        session.add(task)
        session.flush()  # 生成主键供 outbox 外键引用
        outbox = _outbox(task.id)
        session.add(outbox)
        session.commit()
        user_id = user.id
        task_id = task.id

    _assert_rejected(lambda: _task(user_id, status="done"), "ck_async_task_status")
    _assert_rejected(lambda: _task(user_id, progress=101), "ck_async_task_progress")
    _assert_rejected(lambda: _task(user_id, processed=-1), "ck_async_task_processed")
    _assert_rejected(lambda: _task(user_id, total=-5), "ck_async_task_total")
    _assert_rejected(lambda: _task(user_id, retry_count=-1), "ck_async_task_retry_count")
    _assert_rejected(lambda: _outbox(task_id, status="zombie"), "ck_task_outbox_status")
    _assert_rejected(lambda: _outbox(task_id, attempts=-1), "ck_task_outbox_attempts")
    # 幂等键唯一：(type, scope_key, idempotency_key)
    duplicate_key = secrets.token_hex(8)
    factory = _session_factory()
    with factory() as probe:
        probe.add(_task(user_id, idempotency_key=duplicate_key))
        probe.commit()
    _assert_rejected(
        lambda: _task(user_id, idempotency_key=duplicate_key), "uq_task_idempotency"
    )


def test_downgrade_and_reupgrade_roundtrip(migrated_db):
    engine = create_db_engine(load_settings(mysql_database=TEST_DATABASE))
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SHOW TABLES")).fetchone()
        _run(migrated_db, "alembic", "downgrade", "base")
        with engine.connect() as connection:
            remaining = {row[0] for row in connection.execute(text("SHOW TABLES"))}
            assert {"user", "async_task", "task_outbox"} & remaining == set()
        _run(migrated_db, "alembic", "upgrade", "head")
        with engine.connect() as connection:
            restored = {row[0] for row in connection.execute(text("SHOW TABLES"))}
            expected = {"user", "auth_session", "async_task", "task_outbox", "operation_log"}
            assert expected <= restored
    finally:
        engine.dispose()


def test_seed_provision_is_idempotent(migrated_db):
    seed_env = {
        **migrated_db,
        "DG_PROVISION_USERNAME": "seedtester",
        "DG_PROVISION_PASSWORD": "seed-password-123456",
        "DG_PROVISION_ROLE": "Member",
    }

    def seed(env):
        output = _run(env, "packages.persistence.seed")
        assert "provisioned user 'seedtester'" in output
        return output

    seed(seed_env)
    seed(seed_env)  # 重复执行不产生重复记录

    session_factory = _session_factory()
    with session_factory() as session:
        rows = session.execute(select(User).where(User.username == "seedtester")).scalars().all()
        assert len(rows) == 1
        assert rows[0].role == "Member"
        assert rows[0].is_active
        assert rows[0].password_hash.startswith("$argon2id$")
        assert verify_password(rows[0].password_hash, "seed-password-123456")

    # 配置变更时同步密码哈希，仍不产生重复记录
    seed({**seed_env, "DG_PROVISION_PASSWORD": "seed-password-654321"})
    with session_factory() as session:
        rows = session.execute(select(User).where(User.username == "seedtester")).scalars().all()
        assert len(rows) == 1
        assert verify_password(rows[0].password_hash, "seed-password-654321")
        assert not verify_password(rows[0].password_hash, "seed-password-123456")
