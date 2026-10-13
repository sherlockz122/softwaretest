from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from apps.api.application import create_app
from apps.api.auth import current_user
from packages.mining.fix import FixService
from packages.mining.fix_rules import RULE_VERSION
from packages.mining.issues import observation
from packages.persistence.models import (
    SCHEMA_HEAD,
    AsyncTask,
    DefectEvidence,
    FixAssessment,
    FixRun,
    GitCommit,
    IssueObservation,
    OperationLog,
    Repository,
)
from packages.repositories.parser import ParseExecutor
from packages.repositories.safety import RepositoryError
from packages.repositories.sync import SyncService
from tests.auth_support import migrate, scratch_database
from tests.repository_support import git
from tests.test_parsing import parse_env  # noqa: F401
from tests.test_parsing import run as run_parse
from tests.test_parsing import start as start_parse
from tests.test_tasks import expire, task_env  # noqa: F401

pytestmark = pytest.mark.integration
POLICY = {"rule_version": RULE_VERSION, "include_medium": True}


class IssueFixture:
    def __init__(self):
        self.calls = []
        self.status = "bug"

    def lookup(self, url, number):
        self.calls.append(number)
        return observation(self.status, number)


@pytest.fixture
def fix_env(parse_env):  # noqa: F811
    settings, connections, _, actors, repo_id, _, bare = parse_env
    head = git("-C", str(bare), "rev-parse", "HEAD").decode().strip()
    git("-C", str(bare), "config", "user.name", "Fix Fixture")
    git("-C", str(bare), "config", "user.email", "fix@example.invalid")
    tree = git("-C", str(bare), "rev-parse", "HEAD^{tree}").decode().strip()
    for message in ("Fix crash", "Refs #18", "Closes #18", 'Revert "Fix crash"'):
        head = git("-C", str(bare), "commit-tree", tree, "-p", head, "-m", message).decode().strip()
    git("-C", str(bare), "update-ref", "refs/heads/main", head)
    with Session(connections.engine) as db, db.begin():
        repo = db.get(Repository, repo_id)
        repo.head_sha = head
        repo.canonical_url = "https://github.com/team/demo.git"
    assert run_parse(parse_env, start_parse(parse_env))
    issues = IssueFixture()
    yield (
        settings,
        connections,
        FixService(settings, connections, issues=issues),
        actors,
        repo_id,
        issues,
        head,
    )


def start(env, key=None, policy=None):
    return env[2].create(env[3][1], env[4], key or str(uuid4()), policy or POLICY, str(uuid4()))[
        "task_id"
    ]


def run(env, task_id):
    token, payload = env[2].tasks.claim(task_id)
    return ParseExecutor(env[0], env[2]).run(task_id, token, payload)


def test_real_git_mysql_fix_golden_recompute_and_review_history(fix_env):
    _, connections, service, actors, repo_id, issues, head = fix_env
    root = start(fix_env)
    assert run(fix_env, root)
    first = service.results(repo_id, root, page_size=100)
    assert first["total"] == 18 and first["run"]["head_sha"] == head
    assert first["run"]["processed"] == first["run"]["total"] == 18
    high = [r for r in first["items"] if any(e["confidence"] == "high" for e in r["evidence"])]
    assert len(high) == 1 and high[0]["candidate"] and issues.calls == [18]
    row = high[0]
    body = {"status": "rejected", "note": "golden review reason", "expected_revision": 0}
    rejected = service.review(actors[1], repo_id, row["id"], body, str(uuid4()))
    assert not rejected["candidate"] and rejected["review_revision"] == 1
    assert service.review(actors[1], repo_id, row["id"], body, str(uuid4()))["review_revision"] == 1
    with pytest.raises(RepositoryError) as error:
        service.review(actors[3], repo_id, row["id"], body, str(uuid4()))
    assert error.value.code == "FIX_REVIEW_CONFLICT"
    service.review(
        actors[1],
        repo_id,
        row["id"],
        dict(body, status="confirmed", expected_revision=1, note="second review reason"),
        str(uuid4()),
    )
    second = start(fix_env, policy=dict(POLICY, include_medium=False))
    issues.status = "not_bug"
    assert run(fix_env, second)
    assert service.results(repo_id, second, page_size=100)["total"] == 18
    old = next(
        r for r in service.results(repo_id, root, page_size=100)["items"] if r["id"] == row["id"]
    )
    assert old["candidate"] and old["review_revision"] == 2
    assert {h["note"] for h in old["review_history"]} == {
        "golden review reason",
        "second review reason",
    }
    assert all(not r["candidate"] for r in service.results(repo_id, second, page_size=100)["items"])
    with Session(connections.engine) as db:
        assert db.get(Repository, repo_id).head_sha == head
        assert db.scalar(select(func.count()).select_from(FixRun)) == 2
        assert db.scalar(select(func.count()).select_from(DefectEvidence)) > 0


def test_api_concurrent_idempotency_viewer_validation_and_safe_dto(fix_env):
    settings, connections, service, actors, repo_id, _, _ = fix_env
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: start(fix_env, "same"), range(2)))
    assert ids[0] == ids[1]
    with pytest.raises(RepositoryError) as error:
        start(fix_env, "same", dict(POLICY, include_medium=False))
    assert error.value.code == "TASK_IDEMPOTENCY_CONFLICT"
    with pytest.raises(RepositoryError):
        SyncService(settings, connections).create(actors[1], repo_id, "sync", str(uuid4()))
    assert run(fix_env, ids[0])
    app = create_app(settings)
    actor = [actors[2]]
    app.dependency_overrides[current_user] = lambda: actor[0]
    path = "/api/v1/repositories/" + repo_id
    with TestClient(app) as api:
        assert (
            api.post(
                path + "/fix-detection", json={}, headers={"Idempotency-Key": "new"}
            ).status_code
            == 403
        )
        results = api.get(path + f"/fix-runs/{ids[0]}/evidence?page_size=100").json()
        assert "storage_key" not in str(results) and "execution_token" not in str(results)
        assert (
            api.patch(
                path + f"/fix-evidence/{results['items'][0]['id']}/review",
                json={"status": "confirmed", "note": "review", "expected_revision": 0},
            ).status_code
            == 403
        )
        actor[0] = actors[1]
        for body in ({"confirmed_bug": True}, {"rule_version": "unknown"}, {"include_medium": 1}):
            assert (
                api.post(
                    path + "/fix-detection", json=body, headers={"Idempotency-Key": "new"}
                ).status_code
                == 422
            )
        assert api.get(path + "/fix-runs?page_size=1").json()["total"] == 1
        assert api.get(path + f"/fix-runs/{ids[0]}/evidence?page_size=101").status_code == 422
        assert (
            api.patch(
                path + f"/fix-evidence/{results['items'][0]['id']}/review",
                json={"status": "confirmed", "note": "   ", "expected_revision": 0},
            ).status_code
            == 422
        )
        assert (
            api.post(path + "/fix-detection", json={}, headers={"Idempotency-Key": "same"}).json()[
                "task_id"
            ]
            == ids[0]
        )


@pytest.mark.parametrize("failure", ["cancel", "lease", "capacity"])
def test_fenced_atomic_batches_resume_and_no_overwrite(fix_env, monkeypatch, failure):
    _, connections, service, actors, repo_id, issues, head = fix_env
    root = start(fix_env)
    original = service.batch
    seen = []

    def partial(task_id, token, expected, records, plan_hash, complete=False):
        if not expected:
            assert original(task_id, token, expected, records, plan_hash, complete=False)
            seen.append(token)
            if failure == "cancel":
                service.tasks.cancel(actors[1], root, str(uuid4()))
            elif failure == "lease":
                expire(connections.engine, "async_task", "lease_until", root)
            elif failure == "capacity":

                def low(*args):
                    raise RepositoryError(422, "REPOSITORY_STORAGE_LOW")

                monkeypatch.setattr(service.storage, "capacity", low)
            return True
        return original(task_id, token, expected, records, plan_hash, complete)

    monkeypatch.setattr(service, "batch", partial)
    assert not run(fix_env, root)
    assert service.results(repo_id, root, page_size=100)["total"] == 10
    if failure == "cancel":
        assert not service.heartbeat(root, seen[0])
    elif failure == "lease":
        expire(connections.engine, "async_task", "lease_until", root)
        service.tasks.reconcile()
    monkeypatch.undo()
    with Session(connections.engine) as db:
        if failure in {"cancel", "capacity"}:
            child = service.tasks.retry(actors[1], root, "retry", str(uuid4()))["task_id"]
        else:
            child = db.scalar(select(AsyncTask.id).where(AsyncTask.retry_of == root))
    assert child and not service.heartbeat(root, seen[0])
    issues.status = "not_bug"
    assert run(fix_env, child)
    results = service.results(repo_id, root, page_size=100)
    assert results["total"] == 18 and results["run"]["processed"] == 18
    # Any previously published observation remains fixed, even if the provider changes.
    with Session(connections.engine) as db:
        assert db.get(Repository, repo_id).head_sha == head
        assert db.scalar(select(func.count()).select_from(FixAssessment)) == 18


def test_batch_rollback_and_final_commit_ack_loss(fix_env, monkeypatch):
    _, connections, service, _, repo_id, _, _ = fix_env
    root = start(fix_env)
    original = service.batch

    def rollback(*args, **kwargs):
        original_flush = Session.flush

        def fail(db, *a, **kw):
            if any(isinstance(r, DefectEvidence) for r in db.new):
                raise RuntimeError("transaction rollback")
            return original_flush(db, *a, **kw)

        with monkeypatch.context() as patch:
            patch.setattr(Session, "flush", fail)
            return original(*args, **kwargs)

    monkeypatch.setattr(service, "batch", rollback)
    with pytest.raises(RuntimeError):
        run(fix_env, root)
    assert service.results(repo_id, root)["total"] == 10
    assert service.results(repo_id, root)["run"]["processed"] == 10
    expire(connections.engine, "async_task", "lease_until", root)
    service.tasks.reconcile()
    with Session(connections.engine) as db:
        child = db.scalar(select(AsyncTask.id).where(AsyncTask.retry_of == root))

    def lost(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get("complete"):
            raise RuntimeError("completion acknowledgement lost")
        return result

    monkeypatch.setattr(service, "batch", lost)
    with pytest.raises(RuntimeError, match="acknowledgement"):
        run(fix_env, child)
    assert service.tasks.detail(child)["status"] == "succeeded"
    assert service.results(repo_id, root, page_size=100)["total"] == 18


def test_review_audit_rollback_and_whole_path_downgrade(fix_env, monkeypatch):
    _, connections, service, actors, repo_id, _, _ = fix_env
    root = start(fix_env)
    assert run(fix_env, root)
    row = service.results(repo_id, root)["items"][0]
    original = Session.flush

    def fail(db, *args, **kwargs):
        if any(isinstance(r, OperationLog) and r.action == "fix.review" for r in db.new):
            raise RuntimeError("audit transaction rejected")
        return original(db, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Session, "flush", fail)
        with pytest.raises(RuntimeError):
            service.review(
                actors[1],
                repo_id,
                row["id"],
                {"status": "confirmed", "note": "audit check", "expected_revision": 0},
                str(uuid4()),
            )
    assert service.results(repo_id, root)["items"][0]["review_revision"] == 0
    for target in (
        "0005_repository_sync",
        "0004_commit_parsing",
        "0003_repositories",
        "0002_tasks",
        "0001_auth",
        "base",
    ):
        with pytest.raises(RuntimeError, match="Downgrade refused"):
            migrate(connections.engine, "downgrade", target)
        with connections.engine.connect() as db:
            assert (
                db.execute(text("SELECT version_num FROM alembic_version")).scalar() == SCHEMA_HEAD
            )
    for sql in (
        "UPDATE fix_run SET processed=-1",
        "UPDATE fix_assessment SET review_status='invalid'",
        "UPDATE defect_evidence SET confidence='invalid'",
        "UPDATE repository SET fix_status='invalid'",
    ):
        with pytest.raises(DBAPIError), connections.engine.begin() as db:
            db.execute(text(sql))


def test_empty_schema_roundtrip_and_existing_parse_upgrade(fix_env):
    _, connections, _, _, repo_id, _, head = fix_env
    with Session(connections.engine) as db:
        ids = list(db.scalars(select(GitCommit.id)))
    migrate(connections.engine, "downgrade", "0005_repository_sync")
    migrate(connections.engine)
    migrate(connections.engine, "check")
    with Session(connections.engine) as db:
        assert db.get(Repository, repo_id).head_sha == head and all(
            db.get(GitCommit, id) for id in ids
        )
    with scratch_database() as (_, engine):
        migrate(engine)
        migrate(engine, "downgrade", "base")
        migrate(engine)
        migrate(engine, "check")


def test_issue_observation_ack_loss_resumes_frozen_external_evidence(fix_env, monkeypatch):
    _, connections, service, _, repo_id, issues, _ = fix_env
    root = start(fix_env)
    original = service.observe

    def lost(*args):
        original(*args)
        raise RuntimeError("observation acknowledgement lost")

    monkeypatch.setattr(service, "observe", lost)
    with pytest.raises(RuntimeError, match="observation acknowledgement"):
        run(fix_env, root)
    with Session(connections.engine) as db:
        assert db.get(IssueObservation, (root, 18)).snapshot["status"] == "bug"
        assert db.get(FixRun, root).processed == 10
    expire(connections.engine, "async_task", "lease_until", root)
    service.tasks.reconcile()
    with Session(connections.engine) as db:
        child = db.scalar(select(AsyncTask.id).where(AsyncTask.retry_of == root))
    issues.status = "not_bug"
    monkeypatch.setattr(service, "observe", original)
    assert run(fix_env, child)
    assert issues.calls == [18]
    assert any(
        e["confidence"] == "high"
        for r in service.results(repo_id, root, page_size=100)["items"]
        for e in r["evidence"]
    )


def test_accepted_head_excludes_unaccepted_union_and_review_pause_kept(fix_env):
    _, connections, service, _, repo_id, _, head = fix_env
    with Session(connections.engine) as db, db.begin():
        commit = db.scalar(select(GitCommit))
        fields = {
            c.name: getattr(commit, c.name)
            for c in GitCommit.__table__.columns
            if c.name not in {"id", "created_at", "sha"}
        }
        db.add(GitCommit(id=str(uuid4()), sha="f" * 40, **fields))
        db.get(Repository, repo_id).sync_status = "requires_review"
    root = start(fix_env)
    assert run(fix_env, root)
    assert service.results(repo_id, root, page_size=100)["total"] == 18
    with Session(connections.engine) as db:
        repo = db.get(Repository, repo_id)
        assert repo.head_sha == head and repo.sync_status == "requires_review"


@pytest.mark.parametrize("parse_env", [False, True], indirect=True)
def test_recent_window_and_empty_history_are_explicit(parse_env):  # noqa: F811
    settings, connections, _, _, repo_id, _, _ = parse_env
    assert run_parse(parse_env, start_parse(parse_env, limit=3))
    service = FixService(settings, connections)
    env = (*parse_env[:2], service, parse_env[3], repo_id)
    root = start(env)
    assert run(env, root)
    result = service.results(repo_id, root)
    assert result["total"] in {0, 3} and result["run"]["history_coverage"] == "recent_window"
    assert service.tasks.detail(root)["status"] == "succeeded"


def test_capacity_rejection_and_issue_request_budget(fix_env, monkeypatch):
    _, connections, service, _, repo_id, issues, _ = fix_env

    def low(*args):
        raise RepositoryError(422, "REPOSITORY_STORAGE_LOW")

    with monkeypatch.context() as patch:
        patch.setattr(service.storage, "capacity", low)
        with pytest.raises(RepositoryError) as error:
            start(fix_env)
        assert error.value.code == "REPOSITORY_STORAGE_LOW"
    assert service.runs(repo_id)["total"] == 0
    root = start(fix_env)
    with Session(connections.engine) as db, db.begin():
        for n in range(1000, 1256):
            db.add(
                IssueObservation(root_task_id=root, number=n, snapshot=observation("not_bug", n))
            )
    assert run(fix_env, root)
    assert not issues.calls
    with Session(connections.engine) as db:
        assert db.get(IssueObservation, (root, 18)).snapshot["status"] == "request_budget"
