"""真实 Worker 任务：原子领取、心跳续租、执行诊断、条件写回（docs/04 第 10.5 节）。"""

import logging
import threading
import time
import uuid

from apps.worker.main import app
from packages.domain.models import AsyncTask
from packages.persistence.db import create_session_factory
from packages.platform.config import Settings, load_settings
from packages.tasks.domain import EXECUTE_TASK_NAME, sanitize_error_message
from packages.tasks.executor import claim_task, finish_task, heartbeat_task, read_status

logger = logging.getLogger("defectguard")

DIAGNOSTICS_SLICE_SECONDS = 0.5


def run_diagnostics(session_factory, task_id: str, duration_seconds: int) -> str:
    """分片执行诊断负载并轮询取消；返回执行结束时的任务状态。"""
    elapsed = 0.0
    while elapsed < duration_seconds:
        with session_factory() as session:
            status = read_status(session, task_id)
        if status != "running":
            return status
        time.sleep(min(DIAGNOSTICS_SLICE_SECONDS, duration_seconds - elapsed))
        elapsed += min(DIAGNOSTICS_SLICE_SECONDS, duration_seconds - elapsed)
    with session_factory() as session:
        return read_status(session, task_id)


def _start_heartbeat(session_factory, settings: Settings, task_id: str, token: str):
    """守护线程按心跳间隔续租；失去执行权（取消/协调器接管）即停止。"""
    stop_event = threading.Event()

    def beat():
        while not stop_event.wait(settings.task_heartbeat_seconds):
            try:
                with session_factory() as session:
                    if not heartbeat_task(session, settings, task_id, token):
                        return
            except Exception:
                logger.exception("TASK_HEARTBEAT_ERROR task_id=%s", task_id)

    thread = threading.Thread(target=beat, name=f"heartbeat-{task_id}", daemon=True)
    thread.start()
    return stop_event, thread


def _stop_heartbeat(stop_event, thread) -> None:
    stop_event.set()
    thread.join(timeout=2)


@app.task(name=EXECUTE_TASK_NAME, bind=True, acks_late=True)
def execute_task(self, task_id: str, settings: Settings | None = None) -> None:
    """消费 outbox 消息执行任务；重复消息与过期消息由原子领取安全终结。"""
    settings = settings or load_settings()
    session_factory = create_session_factory(settings)
    token = str(uuid.uuid4())
    with session_factory() as session:
        if not claim_task(session, settings, task_id, token):
            return  # 重复消息/已取消/已终态：安全结束
        task = session.get(AsyncTask, task_id)
        duration = int(task.payload.get("duration_seconds", 1))
        actor_id = task.actor_id
    stop_event, thread = _start_heartbeat(session_factory, settings, task_id, token)
    try:
        status = run_diagnostics(session_factory, task_id, duration)
        if status == "running":  # 正常完成
            with session_factory() as session:
                finish_task(session, task_id, token, succeeded=True,
                            result_json={"duration_seconds": duration, "completed": True},
                            actor_id=actor_id)
        elif status == "cancel_requested":  # 成功提交失败，走取消检查点
            with session_factory() as session:
                finish_task(session, task_id, token, succeeded=True,
                            result_json=None, actor_id=actor_id)
        else:  # failed/cancelled 等：失去执行权，不改写
            logger.warning("TASK_EXECUTION_ABANDONED task_id=%s status=%s", task_id, status)
    except Exception:
        logger.exception("TASK_EXECUTION_FAILED task_id=%s", task_id)
        with session_factory() as session:
            finish_task(session, task_id, token, succeeded=False,
                        error_code="TASK_EXECUTION_FAILED",
                        error_message=sanitize_error_message("任务执行失败"),
                        actor_id=actor_id)
    finally:
        _stop_heartbeat(stop_event, thread)
