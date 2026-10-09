import os
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from apps.api.application import create_app
from apps.api.auth import current_user
from packages.persistence.models import (
    SCHEMA_HEAD,
    AsyncTask,
    AuthorIdentity,
    FileChange,
    GitCommit,
    ParseCheckpoint,
    Repository,
)
from packages.repositories.parse_policy import PARSE_GROWTH, PARSER_VERSION
from packages.repositories.parser import ParseExecutor, identity
from packages.repositories.parsing import ParsingService
from packages.repositories.safety import RepositoryError
from packages.repositories.storage import GIB, Storage
from tests.auth_support import migrate, scratch_database
from tests.parsing_support import history
from tests.repository_support import git
from tests.test_tasks import expire, task_env  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("email", [True, False])
def test_identity_normalization_keeps_malformed_bytes_distinct(email):
    if email:
        first, second = b"Name <bad\xff@example.invalid>", b"Name <bad\xfe@example.invalid>"
    else:
        first, second = b"Bad\xff <>", b"Bad\xfe <>"
    identities = [identity(value + b" 1600000000 +0000")[0] for value in (first, second)]
    assert identities[0]["identity_key"] != identities[1]["identity_key"]
    assert "@" not in str(identities)
    assert (
        identity(b"Name < AUTHOR@Example.invalid > 1600000000 +0000")[0]["identity_key"]
        == identity(b"Other <author@example.invalid> 1600000000 +0000")[0]["identity_key"]
    )


@pytest.fixture
def parse_env(task_env, tmp_path, request):  # noqa: F811
    settings, connections, tasks, actors = task_env
    root = tmp_path / "repositories"
    root.mkdir()
    settings = settings.model_copy(update={"repository_storage_root": root})
    source, shas = history(tmp_path / "source", empty=getattr(request, "param", False))
    repository_id = str(uuid4())
    attempt = Storage(settings).attempt(repository_id, str(uuid4()))
    bare = attempt / "repo.git"
    git("clone", "--bare", str(source), str(bare))
    with Session(connections.engine) as db, db.begin():
        task = tasks.new_task(
            db,
            actors[1].id,
            "fixture-clone",
            {"repository_id": repository_id},
            "fixture",
            str(uuid4()),
            kind="repository.clone",
        )
        task.status = task.stage = "succeeded"
        db.add(
            Repository(
                id=repository_id,
                owner_id=actors[1].id,
                canonical_url="https://repo.example/team/demo",
                latest_task_id=task.id,
                status="cloned",
                default_branch=None if not shas else "main",
                head_sha=None if not shas else shas["iteration-5"],
                size_bytes=1,
                storage_key=bare.relative_to(root).as_posix(),
            )
        )
    yield (
        settings,
        connections,
        ParsingService(settings, connections),
        actors,
        repository_id,
        shas,
        bare,
    )


def start(env, limit=None, key="parse-key"):
    return env[2].create(env[3][1], env[4], key, limit, str(uuid4()))["task_id"]


def run(env, task_id):
    token, payload = env[2].tasks.claim(task_id)
    return ParseExecutor(env[0], env[2]).run(task_id, token, payload)


def test_native_git_golden_metadata_files_and_privacy(parse_env):
    settings, connections, service, _, repo_id, shas, bare = parse_env
    assert run(parse_env, start(parse_env))
    expected = sorted(
        (int(timestamp), sha)
        for timestamp, sha in (
            line.split()
            for line in git("-C", str(bare), "log", "--format=%ct %H").decode().splitlines()
        )
    )
    listing = service.listing(repo_id)
    assert [item["sha"] for item in listing["items"]] == [sha for _, sha in expected]
    assert listing["total"] == 14
    with Session(connections.engine) as db:
        commits = {commit.sha: commit for commit in db.scalars(select(GitCommit))}
        author = db.scalar(select(AuthorIdentity))
        assert db.scalar(select(func.count()).select_from(AuthorIdentity)) == 1
        assert "@" not in author.email_hash and len(author.email_hash) == 64
        root = commits[shas["root"]]
        assert root.author_offset == 480 and root.committer_offset == -300
        assert root.author_time == root.committer_time and root.parents == []
        assert commits[shas["empty-message"]].message == ""
        merge = commits[shas["merge"]]
        assert (
            len(merge.parents) == merge.parent_count == 2 and merge.parse_status == "merge_skipped"
        )
        assert not db.scalar(select(FileChange).where(FileChange.commit_id == merge.id))
        files = {
            change.new_path: change
            for change in db.scalars(select(FileChange).where(FileChange.commit_id == root.id))
        }
        assert files["binary.bin"].is_binary and files["binary.bin"].content_status == "binary"
        assert (
            files["大文件.txt"].content_status == "blob_limit"
            and files["大文件.txt"].insertions == 1
        )
        assert (
            files["diff.txt"].content_status == "diff_limit"
            and files["diff.txt"].insertions == 14000
        )
        assert files["old.txt"].insertions == 3 and files["old.txt"].old_loc == 0
        renamed = db.scalar(
            select(FileChange).where(FileChange.commit_id == commits[shas["rename"]].id)
        )
        assert (renamed.change_type, renamed.old_path, renamed.new_path, renamed.insertions) == (
            "R",
            "old.txt",
            "renamed.txt",
            0,
        )
        deleted = db.scalar(
            select(FileChange).where(FileChange.commit_id == commits[shas["delete"]].id)
        )
        assert (
            deleted.change_type == "D"
            and deleted.new_path is None
            and deleted.old_loc == 3
            and deleted.deletions == 3
        )
        modified = db.scalar(
            select(FileChange).where(
                FileChange.commit_id == commits[shas["modify"]].id,
                FileChange.new_path == "renamed.txt",
            )
        )
        assert (modified.insertions, modified.deletions, modified.old_loc) == (2, 2, 3)
        assert modified.line_numbers == {"added": [2, 3], "deleted": [2, 3]}
        assert "++content  " in modified.diff_text
        point = db.scalar(select(ParseCheckpoint))
        assert point.processed == point.total == 14 and point.last_sha == expected[-1][1]
        assert db.get(Repository, repo_id).parse_status == "parsed"
    assert bare.exists() and len(list(settings.repository_storage_root.glob("objects/*/*"))) == 1
    assert "email_hash" not in str(listing) and "AUTHOR@" not in str(listing)
    migrate(connections.engine, "check")


def test_batch_rollback_recovery_old_token_and_duplicate_checkpoint(parse_env, monkeypatch):
    _, connections, service, actors, repo_id, _, bare = parse_env
    task_id = start(parse_env)
    original = service.batch
    attempted = []

    def interrupted(*args, **kwargs):
        attempted.append(args[2])
        if len(attempted) == 2:
            raise RuntimeError("worker interrupted")
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "batch", interrupted)
    token, payload = service.tasks.claim(task_id)
    with pytest.raises(RuntimeError, match="interrupted"):
        ParseExecutor(parse_env[0], service).run(task_id, token, payload)
    with Session(connections.engine) as db:
        point = db.scalar(select(ParseCheckpoint))
        assert (
            point.processed == 10 and db.scalar(select(func.count()).select_from(GitCommit)) == 10
        )
        plan_hash = point.plan_hash
    expire(connections.engine, "async_task", "lease_until", task_id)
    assert not original(task_id, token, 10, [], plan_hash)
    service.tasks.reconcile()
    with Session(connections.engine) as db:
        successor = db.get(Repository, repo_id).latest_task_id
    monkeypatch.setattr(service, "batch", original)
    assert run(parse_env, successor)
    assert not original(task_id, token, 10, [], plan_hash)
    with Session(connections.engine) as db:
        assert db.scalar(select(ParseCheckpoint)).processed == 14
        assert db.scalar(select(func.count()).select_from(GitCommit)) == 14
        assert db.scalar(select(func.count()).select_from(AuthorIdentity)) == 1
    assert bare.exists()


def test_batch_transaction_failure_does_not_advance_or_publish_partial_rows(parse_env, monkeypatch):
    _, connections, service, _, _, _, _ = parse_env
    task_id = start(parse_env)
    original = Session.flush
    fired = [False]

    def fail(db, *args, **kwargs):
        if (
            any(isinstance(row, ParseCheckpoint) and row.processed for row in db.dirty)
            and not fired[0]
        ):
            fired[0] = True
            raise RuntimeError("batch database failure")
        return original(db, *args, **kwargs)

    monkeypatch.setattr(Session, "flush", fail)
    with pytest.raises(RuntimeError, match="batch database failure"):
        run(parse_env, task_id)
    with Session(connections.engine) as db:
        assert db.scalar(select(ParseCheckpoint)).processed == 0
        for model in (GitCommit, FileChange, AuthorIdentity):
            assert db.scalar(select(func.count()).select_from(model)) == 0


def test_cancel_retry_retains_batch_and_unknown_commit_outcome(parse_env, monkeypatch):
    _, connections, service, actors, _, _, bare = parse_env
    task_id = start(parse_env)
    original = service.batch

    def cancel_after_batch(*args, **kwargs):
        result = original(*args, **kwargs)
        service.tasks.cancel(actors[1], task_id, str(uuid4()))
        return result

    monkeypatch.setattr(service, "batch", cancel_after_batch)
    assert not run(parse_env, task_id)
    with Session(connections.engine) as db:
        assert db.scalar(select(ParseCheckpoint)).processed == 10
        assert db.get(AsyncTask, task_id).status == "cancelled"
    child = service.tasks.retry(actors[1], task_id, "retry-key", str(uuid4()))["task_id"]

    def uncertain_commit(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("commit acknowledgement lost")

    monkeypatch.setattr(service, "batch", uncertain_commit)
    with pytest.raises(RuntimeError, match="acknowledgement"):
        run(parse_env, child)
    with Session(connections.engine) as db:
        assert db.scalar(select(ParseCheckpoint)).processed == 14
        assert db.get(AsyncTask, child).status == "succeeded"
    assert bare.exists()


@pytest.mark.parametrize("parse_env", [True], indirect=True)
def test_empty_repository_zero_total_success(parse_env):
    task_id = start(parse_env)
    assert run(parse_env, task_id)
    with Session(parse_env[1].engine) as db:
        task, point = db.get(AsyncTask, task_id), db.scalar(select(ParseCheckpoint))
        assert (
            task.status == "succeeded"
            and task.total == task.processed == 0
            and task.progress == 100
        )
        assert point.head_sha is None and point.last_sha is None


def test_recent_window_idempotency_and_real_api_permissions(parse_env, monkeypatch):
    settings, connections, service, actors, repo_id, _, _ = parse_env
    monkeypatch.setattr("apps.api.repositories.parsing", lambda _: service)
    app = create_app(settings)
    identity = [actors[2]]
    app.dependency_overrides[current_user] = lambda: identity[0]
    path = f"/api/v1/repositories/{repo_id}"
    with TestClient(app) as api:
        assert (
            api.post(path + "/parse", json={}, headers={"Idempotency-Key": "same"}).status_code
            == 403
        )
        identity[0] = actors[1]
        for body in (
            {"commit_limit": 0},
            {"commit_limit": True},
            {"commit_limit": "3"},
            {"unknown": 1},
        ):
            assert (
                api.post(
                    path + "/parse", json=body, headers={"Idempotency-Key": "same"}
                ).status_code
                == 422
            )
        response = api.post(
            path + "/parse", json={"commit_limit": 3}, headers={"Idempotency-Key": "same"}
        )
        assert response.status_code == 202
        task_id = response.json()["task_id"]
        assert (
            api.post(
                path + "/parse", json={"commit_limit": 3}, headers={"Idempotency-Key": "same"}
            ).json()["task_id"]
            == task_id
        )
        assert (
            api.post(path + "/parse", json={}, headers={"Idempotency-Key": "same"}).status_code
            == 409
        )
        assert run(parse_env, task_id)
        identity[0] = actors[2]
        assert api.get(path + "/commits?page_size=2").json()["total"] == 3
        assert len(api.get(path + "/commits?page=2&page_size=2").json()["items"]) == 1
        sha = api.get(path + "/commits?page_size=1").json()["items"][0]["sha"]
        changes = api.get(path + "/commits/" + sha + "/files?page_size=1")
        assert changes.status_code == 200 and changes.json()["total"] == 1
        assert changes.json()["items"][0]["new_path"] == "main.txt"
        assert api.get(path + "/commits/" + "0" * 40 + "/files").status_code == 404
        assert api.get(path + "/commits/not-a-sha/files").status_code == 422
        assert api.get(path + "/commits?page=0").status_code == 422
        app.dependency_overrides.clear()
        assert api.get(path + "/commits").status_code == 401


def test_concurrent_parse_and_database_constraints_nonempty_downgrade(parse_env):
    _, connections, service, actors, repo_id, _, _ = parse_env
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: start(parse_env), range(2)))
    assert ids[0] == ids[1]
    with pytest.raises(RepositoryError):
        start(parse_env, key="different")
    with Session(connections.engine) as db:
        assert db.scalar(select(func.count()).select_from(ParseCheckpoint)) == 1
    for sql in (
        "UPDATE parse_checkpoint SET processed=-1",
        "UPDATE repository SET parse_status='bogus'",
        "UPDATE parse_checkpoint SET processed=2,total=1",
    ):
        with pytest.raises(DBAPIError), connections.engine.begin() as db:
            db.execute(text(sql))
    for target in ("0003_repositories", "0002_tasks", "0001_auth", "base"):
        with pytest.raises(RuntimeError, match="Downgrade refused"):
            migrate(connections.engine, "downgrade", target)
        with connections.engine.connect() as db:
            assert (
                db.execute(text("SELECT version_num FROM alembic_version")).scalar() == SCHEMA_HEAD
            )


def test_low_storage_prevents_acceptance_and_batch_preserves_git(parse_env):
    settings, connections, service, actors, repo_id, _, bare = parse_env
    low = Storage(settings, usage=lambda _: SimpleNamespace(free=2 * GIB + PARSE_GROWTH - 1))
    service.storage = low
    with pytest.raises(RepositoryError) as failure:
        start(parse_env)
    assert failure.value.code == "REPOSITORY_STORAGE_LOW"
    with Session(connections.engine) as db:
        assert not db.scalar(select(ParseCheckpoint))
    service.storage = Storage(settings)
    task_id = start(parse_env)
    service.storage = low
    assert not run(parse_env, task_id)
    with Session(connections.engine) as db:
        assert db.get(AsyncTask, task_id).error_code == "REPOSITORY_STORAGE_LOW"
        assert db.scalar(select(ParseCheckpoint)).processed == 0
    assert bare.exists()


@pytest.mark.parametrize("parse_env", [True], indirect=True)
def test_raw_git_paths_encoding_symlink_and_clean_environment(parse_env, tmp_path, monkeypatch):
    settings, connections, service, _, repo_id, _, bare = parse_env
    blob = git(
        "-C", str(bare), "hash-object", "-w", "--stdin", data=b"text  \ndiff --git literal\n"
    ).strip()
    invalid_blob = git(
        "-C", str(bare), "hash-object", "-w", "--stdin", data=b"nonutf8\xff\n"
    ).strip()
    link = git("-C", str(bare), "hash-object", "-w", "--stdin", data=b"../../outside").strip()
    names = [b"tab\tname.txt", b"line\nname.txt", b"bad\xff.txt", b"@@ file.txt"]
    tree = git(
        "-C",
        str(bare),
        "mktree",
        "-z",
        data=b"".join(b"100644 blob " + blob + b"\t" + name + b"\0" for name in names)
        + b"100644 blob "
        + invalid_blob
        + b"\tencoding.txt\0"
        + b"120000 blob "
        + link
        + b"\tsymlink\0",
    ).strip()
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="Raw",
        GIT_AUTHOR_EMAIL="raw@example.invalid",
        GIT_COMMITTER_NAME="Raw",
        GIT_COMMITTER_EMAIL="raw@example.invalid",
        GIT_AUTHOR_DATE="1600000000 +0000",
        GIT_COMMITTER_DATE="1600000000 +0000",
    )
    head = (
        git("-C", str(bare), "commit-tree", tree.decode(), "-m", "raw names", env=env)
        .decode()
        .strip()
    )
    with Session(connections.engine) as db, db.begin():
        db.get(Repository, repo_id).head_sha = head
    # Commands must ignore inherited external diff and global/system configuration.
    marker = tmp_path / "external-ran"
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", str(marker))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "diff.external")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(marker))
    assert run(parse_env, start(parse_env))
    with Session(connections.engine) as db:
        changes = {row.new_path: row for row in db.scalars(select(FileChange))}
        assert "tab\tname.txt" in changes and "line\nname.txt" in changes
        assert changes["bad\ufffd.txt"].content_status == "encoding"
        assert changes["encoding.txt"].content_status == "encoding"
        assert changes["symlink"].insertions == 1
        assert changes["@@ file.txt"].content_status == "parsed"
        assert changes["@@ file.txt"].line_numbers == {"added": [1, 2], "deleted": []}
    assert not marker.exists() and not (bare / "symlink").exists()


def test_database_unique_foreign_keys_and_empty_roundtrip(parse_env):
    _, connections, service, _, _, _, _ = parse_env
    assert run(parse_env, start(parse_env))
    with Session(connections.engine) as db:
        commits = db.scalars(select(GitCommit).limit(2)).all()
        first_id, second_sha = commits[0].id, commits[1].sha
        change = db.scalar(select(FileChange))
        change_id = change.id
    for sql, params in (
        ("UPDATE git_commit SET sha=:sha WHERE id=:id", {"id": first_id, "sha": second_sha}),
        (
            "UPDATE file_change SET commit_id=:missing WHERE id=:id",
            {"id": change_id, "missing": str(uuid4())},
        ),
        ("UPDATE file_change SET insertions=-1 WHERE id=:id", {"id": change_id}),
    ):
        with pytest.raises(DBAPIError), connections.engine.begin() as db:
            db.execute(text(sql), params)
    with scratch_database() as (_, engine):
        migrate(engine)
        migrate(engine, "check")
        migrate(engine, "downgrade", "base")
        migrate(engine)
        migrate(engine, "check")


def test_changed_plan_old_version_and_capacity_between_batches(parse_env, monkeypatch):
    _, connections, service, actors, repo_id, _, bare = parse_env
    task_id = start(parse_env)
    token, payload = service.tasks.claim(task_id)
    assert service.prepare(task_id, token, "a" * 64, 14) == (0, None)
    with pytest.raises(RepositoryError) as failure:
        service.prepare(task_id, token, "b" * 64, 14)
    assert failure.value.code == "REPOSITORY_PARSE_PLAN_CONFLICT"
    with Session(connections.engine) as db, db.begin():
        point = db.get(ParseCheckpoint, task_id)
        point.parser_version = "unsupported-v2"
    with pytest.raises(RepositoryError) as failure:
        service.heartbeat(task_id, token)
    assert failure.value.code == "REPOSITORY_PARSE_VERSION_CONFLICT"
    with Session(connections.engine) as db, db.begin():
        point = db.get(ParseCheckpoint, task_id)
        point.parser_version, point.plan_hash = PARSER_VERSION, None
    original = service.batch

    def no_space_after_batch(*args, **kwargs):
        result = original(*args, **kwargs)
        service.storage.usage = lambda _: SimpleNamespace(free=2 * GIB + PARSE_GROWTH - 1)
        return result

    monkeypatch.setattr(service, "batch", no_space_after_batch)
    assert not ParseExecutor(parse_env[0], service).run(task_id, token, payload)
    with Session(connections.engine) as db:
        assert db.scalar(select(ParseCheckpoint)).processed == 10
        assert db.get(AsyncTask, task_id).error_code == "REPOSITORY_STORAGE_LOW"
    assert bare.exists()


@pytest.mark.parametrize("failure", ["timeout", "index_limit", "lease"])
def test_supervised_read_failures_leave_no_partial_batch(parse_env, monkeypatch, failure):
    _, connections, service, _, _, _, bare = parse_env
    task_id = start(parse_env)
    if failure == "index_limit":
        monkeypatch.setattr("packages.repositories.parser.INDEX_LIMIT", 1)
    if failure == "timeout":
        monkeypatch.setattr("packages.repositories.parser.time.monotonic", lambda: 100)
        service.settings = service.settings.model_copy(
            update={"repository_parse_timeout_seconds": -1}
        )
    if failure == "lease":
        original = service.heartbeat

        def expire_once(task, token):
            expire(connections.engine, "async_task", "lease_until", task)
            return original(task, token)

        monkeypatch.setattr(service, "heartbeat", expire_once)
    assert not run((service.settings, *parse_env[1:]), task_id)
    with Session(connections.engine) as db:
        assert db.scalar(select(ParseCheckpoint)).processed == 0
        assert db.scalar(select(func.count()).select_from(GitCommit)) == 0
        task = db.get(AsyncTask, task_id)
        assert (
            task.error_code
            == {
                "timeout": "REPOSITORY_PARSE_TIMEOUT",
                "index_limit": "REPOSITORY_PARSE_RESOURCE_LIMIT",
                "lease": None,
            }[failure]
        )
    assert bare.exists()
