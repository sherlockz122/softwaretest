"""Evidence-bound human review handoff and deliberately non-authorizing readiness reports."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from packages.mining import asset_reviews as reviews
from packages.mining import public_assets as assets
from packages.mining.source_evidence import content_hash
from packages.semantic import artifacts, policy
from packages.semantic.control import checkpoint
from packages.semantic.policy import SemanticError


def license_evidence(path):
    value = assets.read_json(assets.safe_path(Path(path)), checkpoint, maximum=1024**2)
    assets.verify_seal(value)
    if (
        value.get("version") != "semantic-license-observation-v1"
        or value.get("model") != policy.MODEL
        or value.get("revision") != policy.REVISION
    ):
        raise SemanticError("ADMISSION_LICENSE_BINDING_DRIFT")
    # Missing metadata cannot authorize publication; outside reviewers still resolve scope.
    if value.get("redistribution_approved") is not False:
        raise SemanticError("ADMISSION_LICENSE_REQUIRES_AUTHORITY")
    return value


def review_bundle(directory, license_path, output):
    directory, output = [assets.safe_path(Path(p)) for p in [directory, output]]
    evidence = license_evidence(license_path)
    initial = reviews.load_initial(directory)
    cases = sorted(p.name for p in (directory / "cases").iterdir() if p.is_dir())
    if len(cases) != 2:
        raise SemanticError("ADMISSION_REVIEW_POPULATION_CHANGED")
    output.mkdir(parents=True, exist_ok=True)
    attempt = output / (".review-" + uuid4().hex)
    attempt.mkdir()
    packets, files = [], []
    for case in cases:
        history = reviews.case_history(directory, case)
        latest = history["records"][-1]
        if latest["revision"] != 2 or latest["status"] != "unresolved":
            raise SemanticError("ADMISSION_REVIEW_REVISION_CHANGED")
        original = history["records"][0]["packet"]
        packet = {
            k: original[k]
            for k in [
                "project",
                "commit_id",
                "display_links",
                "display_links_truncated",
                "raw_links_digest",
                "negative_coverage",
            ]
        }
        refs = []
        for i, item in enumerate(latest["evidence"]):
            source = assets.safe_path(assets.ROOT / item["path"])
            if artifacts.file_digest(source) != {k: item[k] for k in ["bytes", "sha256"]}:
                raise SemanticError("ADMISSION_REVIEW_EVIDENCE_DRIFT")
            name = f"evidence-{case[:16]}-{i}.json"
            (attempt / name).write_bytes(source.read_bytes())
            refs.append(name)
            files.append(name)
        packets.append(
            {
                "case_id": case,
                "hypothesis": packet,
                "source_revision_hash": latest["content_hash"],
                "status": "unresolved",
                "evidence": refs,
            }
        )
    payload = {
        "version": "two-case-source-protocol-review-v1",
        "binding": initial["binding"],
        "cases": packets,
        "model_predictions_included": False,
        "published_label_field_included": False,
        "source_hypothesis_is_visible": True,
        "probability_sample_or_gold": False,
    }
    packet_hash = content_hash(payload)
    assets.write_json(attempt / "packet.json", payload)
    files.append("packet.json")
    for reviewer in ["A", "B"]:
        name = "reviewer-" + reviewer + ".json"
        assets.write_json(
            attempt / name,
            {
                "packet_hash": packet_hash,
                "reviewer_id": None,
                "independent_attestation": False,
                "completed": False,
                "allowed_conclusions": ["supported", "contradicted", "insufficient"],
                "decisions": [
                    {"case_id": c, "conclusion": None, "evidence_refs": [], "rationale": None}
                    for c in cases
                ],
            },
        )
        files.append(name)
    protocol = """# 两条来源假设的独立复核

请由两位了解Git/SZZ与Java变更分析、未共同讨论结论的审阅者各自完成A/B表，每人预估30至60分钟。
核对bug/fix对象、原生双时钟/严格祖先、原issue及AST/clean筛选依据、覆盖/删失；引用具体SHA/issue/行范围和取得时间。
证据不足填insufficient并写缺口；没有找到反例不能证明clean。不得把合成材料、模型分数或发布标签当人工结论。
两表收齐后由第三人或共同记录的仲裁者处理分歧，保留原表和理由，不覆盖旧revision。
这是两条已知来源假设的复核，bug/fix关系本身可见；表中不提供模型预测/指标，不能称完全盲标、概率抽样或噪声率估计。
本批仅导出空表，无审阅完成或gold认证。返回表须另按身份/独立性/证据/仲裁协议验证后，由授权应用流程追加新修订。
后续金标需另预声明独立概率样本、未知处理、支持数及一致性/区间；这两条不是代表性样本。
"""
    protocol += (
        "\n封存目录及其中A/B空表保持只读。请先复制模板到新的响应目录 "
        f"runtime/review-responses/{packet_hash}/A.json 和 B.json，"
        "在副本填写，不修改封存工件。不要将SQL、凭据或个人联系方式写入材料。\n"
    )
    (attempt / "REVIEW.md").write_text(protocol, encoding="utf-8", newline="\n")
    files.append("REVIEW.md")
    return artifacts.publish(
        attempt,
        output,
        {
            "version": payload["version"],
            "packet_hash": packet_hash,
            "case_count": 2,
            "independent_reviews_completed": 0,
            "old_reviews_mutated": False,
            "license_observation": evidence["content_hash"],
            "observed_at": datetime.now(UTC).isoformat(),
            "sources": artifacts.sources(
                [
                    "packages/semantic/admission_review.py",
                    "scripts/semantic_admission.py",
                    "packages/mining/asset_reviews.py",
                ]
            ),
        },
        files,
    )


def readiness(contexts, reservation, cache, bundle, license_path, output):
    import numpy as np

    contexts, reservation, cache, bundle, output = [
        assets.safe_path(Path(p)) for p in [contexts, reservation, cache, bundle, output]
    ]
    manifests = {
        k: artifacts.verify(p, checkpoint)
        for k, p in [
            ("contexts", contexts),
            ("reservation", reservation),
            ("cache", cache),
            ("review_bundle", bundle),
        ]
    }
    license_record = license_evidence(license_path)
    c, r, e, b = [manifests[k] for k in ["contexts", "reservation", "cache", "review_bundle"]]
    if (
        c.get("version") != "hunk-context-v2"
        or r.get("version") != "unlabeled-future-reservation-v1"
        or e.get("binding", {}).get("inputs") != c["content_hash"]
        or b.get("license_observation") != license_record["content_hash"]
    ):
        raise SemanticError("ADMISSION_READINESS_BINDING_DRIFT")
    if (
        e.get("shape") != [c["accepted"], 3076]
        or r.get("holdout_ready") is not False
        or b.get("independent_reviews_completed") != 0
    ):
        raise SemanticError("ADMISSION_SCOPE_DRIFT")
    rows = list(artifacts.records(contexts))
    if len(rows) != c["accepted"] or any(
        v["member"]["partition"] not in {"train", "validation"} for v in rows
    ):
        raise SemanticError("ADMISSION_CONTEXT_POPULATION_DRIFT")
    index = assets.read_json(cache / "rows.json", checkpoint, maximum=4 * 1024**2)["records"]
    values = np.load(cache / "embeddings.npy", allow_pickle=False, mmap_mode="r")
    if (
        index != [{"member": v["member"], "text_hash": v["text_hash"]} for v in rows]
        or values.dtype != np.float32
        or list(values.shape) != e["shape"]
        or not np.isfinite(values).all()
        or e["binding"]["checkpoint"] != c["checkpoint_manifest"]
    ):
        raise SemanticError("ADMISSION_CACHE_CONTENT_DRIFT")
    coverage = assets.read_json(cache / "coverage.json", checkpoint, maximum=4 * 1024**2)["records"]
    if [v["identity"] for v in coverage] != [v["member"]["commit_id"] for v in rows]:
        raise SemanticError("ADMISSION_COVERAGE_MEMBER_DRIFT")
    audit = assets.read_json(contexts / "audit.json", checkpoint)
    selected_omitted = {
        role: sum(v["roles"][role]["omitted_tokens"] for v in coverage) for role in policy.ROLES
    }
    checks = {
        "selected_contexts_fully_encoded": all(v == 0 for v in selected_omitted.values()),
        "candidate_hunk_context_represented": audit["hunks_beyond_cap"] == 0
        and all(v["unrepresented_hunks"] == 0 for v in audit["roles"].values()),
        "new_context_cross_split_duplicates_absent": audit["cross_split_exact_duplicate_groups"]
        == 0,
        "calibration_and_threshold_minimum_floor": c["supports"][
            "calibration_and_threshold_floor_met"
        ],
        "independent_labeled_holdout": False,
        "authoritative_weight_license_scope": False,
        "independent_source_review_completed": False,
        "pretraining_near_duplicate_audit": False,
        "nfr05_raw_artifact_byte_determinism": False,
        "semantic_product_task_api_ui_and_inference": False,
    }
    report = {
        "version": "semantic-admission-report-v1",
        "checks": checks,
        "blocked_conditions": [k for k, passed in checks.items() if not passed],
        "candidate_release": False,
        "full_product_p1_complete": False,
        "all_historical_or_potential_issues_guaranteed_resolvable": False,
        "new_test_scores_or_classifier_fits": 0,
        "selected_context_omitted_tokens": selected_omitted,
        "user_help": [
            {
                "request": "two independent source-protocol reviewers, then adjudication",
                "estimated_minutes_per_reviewer": [30, 60],
                "material": "review bundle REVIEW.md and reviewer-A/B.json",
                "blocking_scope": "independent source certification, not this engineering delivery",
            },
            {
                "request": (
                    "authoritative weight-license scope evidence or an explicitly licensed "
                    "checkpoint before redistribution"
                ),
                "blocking_scope": "weight/derivative distribution and product release",
            },
        ],
        "limits": [
            "a floor of ten positives is only an engineering guard, not statistical adequacy",
            "context coverage is not whole-commit coverage or predictive gain",
            "reserved identities have no labels or release qualification",
        ],
    }
    output.mkdir(parents=True, exist_ok=True)
    attempt = output / (".readiness-" + uuid4().hex)
    attempt.mkdir()
    assets.write_json(attempt / "report.json", report)
    return artifacts.publish(
        attempt,
        output,
        {
            "version": report["version"],
            "bindings": {k: m["content_hash"] for k, m in manifests.items()},
            "license_observation": license_record["content_hash"],
            "candidate_release": False,
            "sources": artifacts.sources(
                [
                    "packages/semantic/admission.py",
                    "packages/semantic/admission_review.py",
                    "scripts/semantic_admission.py",
                ]
            ),
        },
        ["report.json"],
    )
