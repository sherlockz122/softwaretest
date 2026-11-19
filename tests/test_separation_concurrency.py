"""训练预测分离与多用户并发契约（真实 MySQL/Redis）。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from packages.auth.security import AuthError
from packages.mining import public_assets as assets
from packages.mining import research_dataset as research
from packages.persistence.models import AsyncTask, ModelRun, TaskOutbox
from packages.tasks.service import TaskError, database_now
from tests.dataset_support import fixture_asset
from tests.test_model_application import claim, database, environment  # noqa: F401

pytestmark = pytest.mark.integration


def task_count(db):
    return db.scalar(select(func.count()).select_from(AsyncTask))


def test_stuck_training_does_not_block_registered_predictions(environment):  # noqa: F811
    service, body, users, _, engine, _ = environment
    # 注册模型 A：训练完成，风险列表可用
    ack_a, token_a, payload_a = claim(environment)
    service.run(ack_a["task_id"], token_a, payload_a)
    assert service.risk_items(ack_a["model_run_id"], 1, 10)["items"]
    # 模型 B 训练任务卡死：租约过期且没有 worker 续期
    ack_b = service.create(users[1], uuid4().hex, body, str(uuid4()))
    with Session(engine) as db, db.begin():
        db.get(AsyncTask, ack_b["task_id"]).lease_until = database_now(db) - timedelta(
            seconds=1
        )
    # B 卡死期间 A 的风险列表照常返回，且不产生任何任务队列消息
    with Session(engine) as db:
        before = (task_count(db), db.scalar(select(func.count()).select_from(TaskOutbox)))
    risk = service.risk_items(ack_a["model_run_id"], 1, 10)
    assert risk["items"]
    with Session(engine) as db:
        after = (task_count(db), db.scalar(select(func.count()).select_from(TaskOutbox)))
    assert before == after


def test_registered_run_has_no_retry_channel(environment):  # noqa: F811
    service, body, users, _, engine, _ = environment
    ack, token, payload = claim(environment)
    service.run(ack["task_id"], token, payload)
    detail = service.detail(ack["model_run_id"])
    assert detail["registered"]
    # 已注册运行的任务不可重试：注册发布是训练到预测的唯一通道
    with pytest.raises(TaskError, match="TASK_STATE_CONFLICT"):
        service.tasks.retry(users[1], ack["task_id"], uuid4().hex, str(uuid4()))
    # 状态查询前后预测结果稳定（工件哈希校验路径不变）
    first = service.risk_items(ack["model_run_id"], 1, 10)
    with Session(engine) as db, db.begin():
        run = db.get(ModelRun, ack["model_run_id"])
        assert run.latest_task_id == ack["task_id"]
    second = service.risk_items(ack["model_run_id"], 1, 10)
    assert first == second


def test_concurrent_same_idempotency_key_creates_one_task(environment):  # noqa: F811
    service, body, users, _, engine, _ = environment
    key = uuid4().hex

    def submit():
        return service.create(users[1], key, body, str(uuid4()))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in [pool.submit(submit) for _ in range(2)]]
    assert results[0]["task_id"] == results[1]["task_id"]
    with Session(engine) as db:
        rows = db.scalars(
            select(AsyncTask).where(
                AsyncTask.idempotency_key == key,
                AsyncTask.type == "model.train",
            )
        ).all()
        assert len(rows) == 1
    # 并发创建的任务照常可执行
    token, payload = service.tasks.claim(results[0]["task_id"])
    service.run(results[0]["task_id"], token, payload)
    assert service.detail(results[0]["model_run_id"])["registered"]


def test_two_users_concurrent_training_isolated(environment):  # noqa: F811
    service, body, users, _, engine, dataset = environment
    # 给第二位 Member 建一份独立数据集
    with TemporaryDirectory(dir=assets.ROOT / "runtime/temp", prefix="two-user-") as name:
        _, source = fixture_asset(
            Path(name), rows=100, projects=("apache/commons-io",), store=research.STORE
        )
        body_b = {
            "name": "second member dataset",
            "asset_id": source["manifest"]["asset_id"],
            "source_hash": source["manifest"]["content_hash"],
            "purpose": research.PURPOSE,
            "split_strategy": "time_ordered",
            "train_ratio": 0.7,
            "validation_ratio": 0.15,
            "test_ratio": 0.15,
        }
        ack_b = dataset.create(users[3], uuid4().hex, body_b, str(uuid4()))
        token_b, payload_b = dataset.tasks.claim(ack_b["task_id"])
        dataset.run(ack_b["task_id"], token_b, payload_b)
        assert dataset.detail(ack_b["dataset_id"])["ready"]

    ack_a = service.create(users[1], uuid4().hex, body, str(uuid4()))
    ack_b = service.create(
        users[3], uuid4().hex, dict(body, dataset_id=ack_b["dataset_id"]), str(uuid4())
    )

    def train(ack):
        token, payload = service.tasks.claim(ack["task_id"])
        service.run(ack["task_id"], token, payload)
        return service.detail(ack["model_run_id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        details = [future.result() for future in [pool.submit(train, a) for a in (ack_a, ack_b)]]
    assert all(item["registered"] for item in details)
    # 产物各归各主：两个运行的工件哈希不同（数据集不同）
    path_a, record_a, _ = service.artifact(ack_a["model_run_id"], "model.skops")
    path_b, record_b, _ = service.artifact(ack_b["model_run_id"], "model.skops")
    assert record_a.sha256 != record_b.sha256
    # 跨用户操作 403：第二位 Member 不能取消第一位 Member 的任务
    with pytest.raises(AuthError, match="AUTH_FORBIDDEN"):
        service.tasks.cancel(users[3], ack_a["task_id"], str(uuid4()))
    # 任务状态不变
    with Session(engine) as db:
        assert db.get(AsyncTask, ack_a["task_id"]).status == "succeeded"


def test_queue_serial_consumption_and_reconcile_recovery(environment):  # noqa: F811
    service, body, users, _, engine, _ = environment
    # 连续提交三个训练：outbox 按提交顺序投递
    acks = [service.create(users[1], uuid4().hex, body, str(uuid4())) for _ in range(3)]
    with Session(engine) as db:
        rows = db.scalars(
            select(TaskOutbox)
            .where(TaskOutbox.task_id.in_([a["task_id"] for a in acks]))
            .order_by(TaskOutbox.next_attempt_at)
        ).all()
        assert [row.task_id for row in rows] == [a["task_id"] for a in acks]
    # 串行消费：按序 claim + run，状态转移合法
    for ack in acks:
        token, payload = service.tasks.claim(ack["task_id"])
        service.run(ack["task_id"], token, payload)
        assert service.detail(ack["model_run_id"])["registered"]
    # 崩溃恢复（执行中租约过期 → reconcile 产出重试任务 → 重试成功）由
    # test_model_application.py::test_expired_token_and_auto_retry_cannot_publish
    # 与 test_subprocess_fault_retry_same_root_and_old_fence 覆盖。
