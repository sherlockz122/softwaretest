"""修复参考检索：从同一仓库真实修复提交里找相似案例（只读，不训练、不联网）。

相似度由三部分共同决定：触及文件的重叠数量（降序）、十四维特征距离（升序，
缺公共维度排后）、修复证据等级（高 > 中 > 低）。检索范围覆盖该仓库已采集的
全部历史，包括时间上晚于目标提交的修复——参考是检索材料，不参与模型训练，
晚到的修复同样可以当例子看。
"""

import math

from sqlalchemy import select

from packages.mining.fix_rules import RULE_VERSION
from packages.persistence.models import (
    AsyncTask,
    DefectEvidence,
    FeatureItem,
    FeatureRun,
    FileChange,
    FixAssessment,
    FixRun,
    GitCommit,
    Repository,
)

LIMIT_DEFAULT = 20
LIMIT_MAX = 100
MESSAGE_LENGTH = 160
FILES_SHOWN = 5
CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}


class RecommendError(ValueError):
    def __init__(self, code, status=422):
        self.code, self.status = code, status
        super().__init__(code)


def completed_root(db, model, repository_id, order):
    rows = db.execute(
        select(model)
        .where(model.repository_id == repository_id)
        .order_by(order.desc())
        .limit(8)
    ).scalars().all()
    return next(
        (r for r in rows if r.total is not None and r.processed == r.total), None
    )


def succeeded(db, root):
    tasks = db.scalars(
        select(AsyncTask)
        .where(AsyncTask.root_task_id == root, AsyncTask.status == "succeeded")
    ).all()
    return bool(tasks)


def fix_candidates(db, repository_id):
    """该仓库全部已采集历史里的候选修复提交（含时间上晚于目标提交的）。"""
    rows = db.execute(
        select(FixAssessment)
        .join(FixRun, FixAssessment.root_task_id == FixRun.root_task_id)
        .where(
            FixRun.repository_id == repository_id,
            FixAssessment.rule_candidate.is_(True),
            FixAssessment.review_status.in_(["confirmed", "unreviewed"]),
        )
    ).scalars().all()
    results = {}
    for assessment in rows:
        run = db.get(FixRun, assessment.root_task_id)
        if (
            run is None
            or run.processed != run.total
            or not succeeded(db, run.root_task_id)
        ):
            continue
        results[assessment.sha] = assessment.id
    return results


def target_files(db, repository_id, sha):
    commit = db.scalar(
        select(GitCommit).where(
            GitCommit.repository_id == repository_id, GitCommit.sha == sha
        )
    )
    if commit is None:
        raise RecommendError("FIX_RECOMMEND_COMMIT_NOT_FOUND", 404)
    changes = db.scalars(
        select(FileChange)
        .where(FileChange.commit_id == commit.id)
        .order_by(FileChange.ordinal)
    ).all()
    return commit, changes


def feature_vector(db, repository_id, sha):
    run = completed_root(db, FeatureRun, repository_id, FeatureRun.created_at)
    if run is None or run.purpose != "historical":
        return None, None
    item = db.scalar(
        select(FeatureItem).where(
            FeatureItem.root_task_id == run.root_task_id, FeatureItem.sha == sha
        )
    )
    if item is None or item.result_json is None:
        return None, None
    return run.root_task_id, item.result_json.get("values")


def vector_distance(a, b):
    """十四维特征距离：只数两边都有值的维度；没有公共维度时为 None。"""
    if not a or not b:
        return None
    shared = [k for k in a if k in b and a[k] is not None and b[k] is not None]
    if not shared:
        return None
    total = sum((float(a[k]) - float(b[k])) ** 2 for k in shared)
    return math.sqrt(total / len(shared))


def recommend(db, repository_id, target_sha, *, limit=LIMIT_DEFAULT):
    """检索相似修复参考；返回 {items, total, limit, rule_version}。"""
    if type(limit) is not int or not 1 <= limit <= LIMIT_MAX:
        raise RecommendError("FIX_RECOMMEND_LIMIT_INVALID")
    repo = db.get(Repository, repository_id)
    if repo is None:
        raise RecommendError("FIX_RECOMMEND_REPOSITORY_NOT_FOUND", 404)
    commit, target_changes = target_files(db, repository_id, target_sha)
    target_values = feature_vector(db, repository_id, target_sha)[1]
    candidates = fix_candidates(db, repository_id)
    candidates.pop(target_sha, None)

    target_hashes = {c.path_bytes_hash for c in target_changes}
    target_paths = {c.new_path or c.old_path for c in target_changes if c.new_path or c.old_path}

    rows = []
    for sha, assessment_id in candidates.items():
        fix_commit = db.scalar(
            select(GitCommit).where(
                GitCommit.repository_id == repository_id, GitCommit.sha == sha
            )
        )
        if fix_commit is None:
            continue
        changes = db.scalars(
            select(FileChange)
            .where(FileChange.commit_id == fix_commit.id)
            .order_by(FileChange.ordinal)
            .limit(FILES_SHOWN + 1)
        ).all()
        shown = [c.new_path or c.old_path for c in changes if c.new_path or c.old_path][
            :FILES_SHOWN
        ]
        overlap = len(target_hashes & {c.path_bytes_hash for c in changes})
        if overlap == 0 and target_paths:
            path_overlap = len(
                target_paths
                & {c.new_path or c.old_path for c in changes if c.new_path or c.old_path}
            )
        else:
            path_overlap = None
        distance = vector_distance(target_values, feature_vector(db, repository_id, sha)[1])
        evidence = db.scalars(
            select(DefectEvidence)
            .where(DefectEvidence.assessment_id == assessment_id)
            .order_by(DefectEvidence.ordinal)
        ).all()
        level = (
            min((CONFIDENCE_RANK.get(e.confidence, 2) for e in evidence), default=None)
            if evidence
            else None
        )
        message = (fix_commit.message or "").strip().splitlines()
        summary = (message[0] if message else "")[:MESSAGE_LENGTH]
        rows.append(
            {
                "fix_sha": sha,
                "message_summary": summary,
                "touched_files": shown,
                "touched_files_shown": len(shown),
                "evidence_level": (
                    {0: "high", 1: "medium", 2: "low"}.get(level) if level is not None else None
                ),
                "similarity": {
                    "file_overlap": overlap,
                    "file_path_overlap": path_overlap,
                    "feature_distance": round(distance, 6) if distance is not None else None,
                    "evidence_rank": level,
                },
            }
        )

    def key(row):
        similarity = row["similarity"]
        return (
            -similarity["file_overlap"],
            -(similarity["file_path_overlap"] or 0),
            similarity["feature_distance"]
            if similarity["feature_distance"] is not None
            else float("inf"),
            similarity["evidence_rank"] if similarity["evidence_rank"] is not None else 3,
            row["fix_sha"],
        )

    rows.sort(key=key)
    total = len(rows)
    return {
        "items": rows[:limit],
        "total": total,
        "limit": limit,
        "rule_version": RULE_VERSION,
        "target": {
            "sha": target_sha,
            "message_summary": (commit.message or "").strip().splitlines()[0][:MESSAGE_LENGTH]
            if (commit.message or "").strip()
            else "",
            "touched_files": [
                c.new_path or c.old_path
                for c in target_changes
                if c.new_path or c.old_path
            ][:FILES_SHOWN],
        },
        "scope": "repository_full_history_including_later_fixes",
    }
