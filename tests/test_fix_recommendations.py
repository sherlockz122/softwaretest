"""修复参考检索：真实 MySQL 下的排序、边界与追溯口径。"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.application import create_app
from packages.mining.fix_recommendations import (
    LIMIT_DEFAULT,
    RecommendError,
    recommend,
    vector_distance,
)
from packages.mining.fix_rules import RULE_VERSION
from packages.persistence.models import (
    FeatureItem,
    FeatureRun,
    FixAssessment,
    GitCommit,
)
from tests.test_fix import fix_env, run, start  # noqa: F401
from tests.test_parsing import parse_env  # noqa: F401
from tests.test_tasks import task_env  # noqa: F401

pytestmark = pytest.mark.integration

FEATURE_NAMES = "la ld nf nd ns ent ndev age nuc aexp arexp asexp".split()


def candidate_shas(db, root):
    return {
        a.sha
        for a in db.scalars(
            select(FixAssessment).where(FixAssessment.root_task_id == root)
        )
        if a.rule_candidate
    }


def test_vector_distance_shared_dims_and_fallbacks():
    assert vector_distance({"a": 0, "b": 2}, {"a": 3, "b": 6}) == pytest.approx(3.5355339059327378)
    assert vector_distance({"a": 0}, {"b": 1}) is None
    assert vector_distance(None, {"a": 1}) is None
    assert vector_distance({"a": 1}, None) is None


def test_recommend_ranks_by_overlap_and_evidence_and_includes_later_fixes(fix_env):  # noqa: F811
    settings, connections, _, _, repo_id, _, _ = fix_env
    root = start(fix_env)
    assert run(fix_env, root)
    engine = connections.engine
    with Session(engine) as db, db.begin():
        candidates = candidate_shas(db, root)
        assert candidates, "fix fixture 应当产出规则候选"
        target = db.scalar(
            select(GitCommit)
            .where(GitCommit.repository_id == repo_id, GitCommit.sha.not_in(candidates))
            .order_by(GitCommit.committer_time)
            .limit(1)
        )
        assert target is not None
        target_sha = target.sha
        result = recommend(db, repo_id, target_sha)
    assert result["rule_version"] == RULE_VERSION
    assert result["scope"] == "repository_full_history_including_later_fixes"
    assert result["target"]["sha"] == target_sha
    assert result["items"], "修复历史里应当有候选修复提交"
    assert all(item["fix_sha"] != target_sha for item in result["items"])
    # 参考只来自该仓库真实候选修复提交
    assert {item["fix_sha"] for item in result["items"]} <= candidates
    # 排序：文件重叠降序在前
    overlaps = [item["similarity"]["file_overlap"] for item in result["items"]]
    assert overlaps == sorted(overlaps, reverse=True)
    for item in result["items"]:
        assert item["message_summary"]
        assert item["similarity"]["file_overlap"] >= 0
        assert "evidence_level" in item
    assert result["total"] == len(result["items"]) <= LIMIT_DEFAULT


def test_recommend_limit_errors_and_empty_history(fix_env):  # noqa: F811
    settings, connections, _, _, repo_id, _, _ = fix_env
    engine = connections.engine
    with Session(engine) as db, db.begin():
        target = db.scalar(
            select(GitCommit).where(GitCommit.repository_id == repo_id).order_by(GitCommit.id)
        )
        assert target is not None
        # 没有跑过 Fix 识别：明确空状态而不是报错或伪造
        empty = recommend(db, repo_id, target.sha)
        assert empty["items"] == [] and empty["total"] == 0
        with pytest.raises(RecommendError) as exc:
            recommend(db, repo_id, target.sha, limit=0)
        assert exc.value.code == "FIX_RECOMMEND_LIMIT_INVALID"
        with pytest.raises(RecommendError) as exc:
            recommend(db, repo_id, target.sha, limit=101)
        assert exc.value.code == "FIX_RECOMMEND_LIMIT_INVALID"
        with pytest.raises(RecommendError) as exc:
            recommend(db, repo_id, "0" * 40)
        assert exc.value.code == "FIX_RECOMMEND_COMMIT_NOT_FOUND" and exc.value.status == 404
        with pytest.raises(RecommendError) as exc:
            recommend(db, str(uuid4()), target.sha)
        assert exc.value.code == "FIX_RECOMMEND_REPOSITORY_NOT_FOUND" and exc.value.status == 404


def test_recommend_uses_feature_distance_when_feature_run_exists(fix_env):  # noqa: F811
    settings, connections, _, _, repo_id, _, head = fix_env
    root = start(fix_env)
    assert run(fix_env, root)
    engine = connections.engine
    with Session(engine) as db, db.begin():
        assessments = [
            a
            for a in db.scalars(
                select(FixAssessment).where(FixAssessment.root_task_id == root)
            )
            if a.rule_candidate
        ]
        target = db.scalar(
            select(GitCommit)
            .where(GitCommit.repository_id == repo_id)
            .order_by(GitCommit.committer_time)
            .limit(1)
        )
        # 造一份最小特征轮次：目标与第一个候选特征相近，与其余候选特征远
        values_close = {name: 1.0 for name in FEATURE_NAMES}
        values_far = {name: 50.0 for name in FEATURE_NAMES}
        db.add(
            FeatureRun(
                root_task_id=root,
                repository_id=repo_id,
                head_sha=head,
                storage_key="objects/probe",
                parser_version="probe",
                identity_version="probe",
                history_coverage="probe",
                feature_version="probe",
                rule_hash="0" * 64,
                purpose="historical",
                fix_json={},
                fix_digest="0" * 64,
                source_digest="0" * 64,
                input_digest="0" * 64,
                quality_json={},
                git_version="probe",
                plan_hash="0" * 64,
                total=2,
                processed=2,
                last_sha=target.sha,
            )
        )
        db.flush()
        items = [
            FeatureItem(
                id=str(uuid4()),
                root_task_id=root,
                ordinal=0,
                sha=target.sha,
                input_json={},
                result_json={"values": values_close},
            ),
            FeatureItem(
                id=str(uuid4()),
                root_task_id=root,
                ordinal=1,
                sha=assessments[0].sha,
                input_json={},
                result_json={"values": values_close},
            ),
        ]
        for ordinal, assessment in enumerate(assessments[1:], start=2):
            items.append(
                FeatureItem(
                    id=str(uuid4()),
                    root_task_id=root,
                    ordinal=ordinal,
                    sha=assessment.sha,
                    input_json={},
                    result_json={"values": values_far},
                )
            )
        db.add_all(items)
        first_sha = assessments[0].sha
        result = recommend(db, repo_id, target.sha, limit=20)
    assert result["items"]
    first = result["items"][0]
    assert first["similarity"]["feature_distance"] is not None
    assert first["fix_sha"] == first_sha


def test_recommend_api_readonly_and_requires_login(fix_env):  # noqa: F811
    settings, connections, _, actors, repo_id, _, _ = fix_env
    root = start(fix_env)
    assert run(fix_env, root)
    with Session(connections.engine) as db:
        target = db.scalar(
            select(GitCommit).where(GitCommit.repository_id == repo_id).order_by(GitCommit.id)
        )
        target_sha = target.sha
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        path = f"/api/v1/repositories/{repo_id}/fix-recommendations"
        assert client.get(path, params={"sha": target_sha}).status_code == 401
        login = lambda index: {  # noqa: E731
            "Authorization": "Bearer "
            + client.post(
                "/api/v1/auth/login",
                json={
                    "username": actors[index].username,
                    "password": "task-fixture-password",
                },
            ).json()["access_token"]
        }
        viewer = login(2)
        response = client.get(path, params={"sha": target_sha}, headers=viewer)
        assert response.status_code == 200
        body = response.json()
        assert set(body) >= {"items", "total", "limit", "rule_version"}
        bad = client.get(path, params={"sha": "0" * 40}, headers=viewer)
        assert bad.status_code == 404
        assert bad.json()["code"] == "FIX_RECOMMEND_COMMIT_NOT_FOUND"
        bad_limit = client.get(path, params={"sha": target_sha, "limit": 0}, headers=viewer)
        assert bad_limit.status_code == 422
