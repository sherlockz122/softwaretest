"""Independent fixed membership, whole-line, drift and resource counterexamples."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pytest

from packages.mining import public_assets as assets
from packages.mining.source_evidence import content_hash
from packages.semantic import admission, artifacts, policy
from packages.semantic import coverage as c
from packages.semantic import coverage_encoder as e
from packages.semantic.policy import SemanticError
from tests.test_semantic_kernel import git_fixture


@pytest.fixture
def work():
    root = assets.ROOT / "runtime/temp"
    root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=root, prefix="coverage-unit-") as name:
        yield Path(name)


def population():
    rows = []
    for part, counts in [("train", (82, 229, 135, 41, 1)), ("validation", (27, 98, 38, 8, 1))]:
        for size, count in zip((1, 2, 9, 33, 129), counts, strict=True):
            for _ in range(count):
                rows.append(
                    {
                        "member": {
                            "canonical_repository": policy.REPOSITORY,
                            "partition": part,
                            "commit_id": f"{len(rows) + 1:040x}",
                            "label": len(rows) % 2,
                        },
                        "context": {"hunks": list(range(size))},
                    }
                )
    return rows


def test_membership_matches_fixed_quotas_is_order_and_label_independent():
    rows = population()
    selected, all_rows = c.selection(rows)
    assert len(selected) == 96 and len(all_rows) == 660
    assert [
        sum(p["partition"] == part and p["stratum"] == i for p in selected)
        for part in c.QUOTAS
        for i in range(5)
    ] == [10, 24, 20, 9, 1, 6, 14, 8, 3, 1]
    changed = [r | {"member": r["member"] | {"label": 999, "score": -999}} for r in reversed(rows)]
    assert c.selection(changed) == (selected, all_rows)
    assert all(
        set(p) == {"canonical_repository", "partition", "commit_id", "hunks", "stratum"}
        for p in selected
    )
    # Earliest SHA of every stratum, never a better-looking label.
    for part in c.QUOTAS:
        for index, count in enumerate(c.QUOTAS[part]):
            expected = sorted(
                p["commit_id"] for p in all_rows if p["partition"] == part and p["stratum"] == index
            )[:count]
            assert [
                p["commit_id"] for p in selected if p["partition"] == part and p["stratum"] == index
            ] == expected


@pytest.mark.parametrize("bad", ["test", "future"])
def test_wrong_partition_rejected(bad):
    rows = population()
    rows[0]["member"]["partition"] = bad
    with pytest.raises(SemanticError, match="OLD_TEST_FORBIDDEN"):
        c.selection(rows)


@pytest.mark.parametrize("bad", ["0" * 40, "g" * 40, "a" * 39, None])
def test_invalid_sha_rejected(bad):
    rows = population()
    rows[0]["member"]["commit_id"] = bad
    with pytest.raises(SemanticError, match="IDENTITY"):
        c.selection(rows)


@pytest.mark.parametrize("failure", ["duplicate", "repository", "population", "rare_stratum"])
def test_no_substitution_when_population_invalid(failure):
    rows = population()
    if failure == "duplicate":
        rows[1]["member"]["commit_id"] = rows[0]["member"]["commit_id"]
    elif failure == "repository":
        rows[0]["member"]["canonical_repository"] = "other/repository"
    elif failure == "population":
        rows.pop()
    else:
        rows[-1]["context"]["hunks"] = [0, 1]
    with pytest.raises(SemanticError):
        c.selection(rows)


@pytest.mark.parametrize(
    "value,expected",
    [(1, 0), (2, 1), (8, 1), (9, 2), (32, 2), (33, 3), (128, 3), (129, 4), (256, 4)],
)
def test_stratum_boundaries(value, expected):
    assert c.stratum(value) == expected


@pytest.mark.parametrize("value", [0, 257, True])
def test_out_of_domain_hunks_do_not_become_easy_examples(value):
    with pytest.raises(SemanticError, match="HUNK_STRATUM"):
        c.stratum(value)


@pytest.mark.parametrize("separator", ["\r\n", "\u2028", "\x0c"])
def test_252_budget_preserves_old_exact_selection_and_physical_coordinates(separator):
    groups, _ = admission.hunk_groups(
        [(f"old{separator}value\nkeep\n", f"new{separator}value\nkeep\n")]
    )
    for role in c.CODE_ROLES:
        assert c.select_lines(groups[role], list, budget=252) == admission.select_lines(
            groups[role], list
        )


def test_bigger_whole_line_budget_improves_fixture_without_fragments():
    groups = [
        {"hunk": i, "eligible_lines": 1, "candidates": [(i, 0, "x" * 30 + "\n")]} for i in range(20)
    ]
    _, baseline = admission.select_lines(groups, list)
    text, current = c.select_lines(groups, list)
    assert current["represented_hunks"] == 20 > baseline["represented_hunks"]
    assert text == "\n".join(["x" * 30 + "\n"] * 20)
    assert len(text) == current["selected_tokens"] <= 1008


def test_expansion_must_not_drop_previously_selected_whole_lines():
    groups = [
        {"hunk": 0, "eligible_lines": 1, "candidates": [(0, 0, "L" * 900 + "\n")]},
        {"hunk": 1, "eligible_lines": 1, "candidates": [(1, 0, "s" * 200 + "\n")]},
    ]
    _, baseline = admission.select_lines(groups, list)
    _, expanded = c.select_lines(groups, list)
    before = {(v["file"], v["line"]) for v in baseline["selected_coordinates"]}
    after = {(v["file"], v["line"]) for v in expanded["selected_coordinates"]}
    assert before <= after


def test_long_line_and_cap_omissions_are_not_complete_hunks():
    groups = [
        {"hunk": 0, "eligible_lines": 65, "candidates": [(0, 0, "x" * 1009)]},
        {"hunk": 1, "eligible_lines": 5, "candidates": [(1, 0, "ok\n")]},
    ]
    text, audit = c.select_lines(groups, list)
    audit = c.line_coverage(groups, audit)
    assert text == "ok\n" and audit["unrepresented_hunks"] == 1
    assert audit["candidate_cap_omitted_lines"] == 68 and audit["complete_eligible_hunks"] == 0
    assert audit["selected_hunk_line_incidences"] == 1


def test_shared_base_line_counts_unique_selection_and_hunk_incidences_separately():
    groups = [
        {"hunk": i, "eligible_lines": 1, "candidates": [(0, 0, "shared\n")]} for i in range(2)
    ]
    _, audit = c.select_lines(groups, list)
    result = c.line_coverage(groups, audit)
    assert len(result["selected_coordinates"]) == 1
    assert result["selected_hunk_line_incidences"] == result["complete_eligible_hunks"] == 2


def test_budget_check_limit_does_not_claim_all_lines():
    groups = [
        {"hunk": i, "eligible_lines": 3, "candidates": [(0, i * 3 + n, "x") for n in range(3)]}
        for i in range(256)
    ]
    _, audit = c.select_lines(groups, list)
    assert audit["budget_checks"] == 512
    assert c.line_coverage(groups, audit)["complete_eligible_hunks"] < 256


@pytest.mark.parametrize("budget", [True, 0, 1009])
def test_invalid_token_budget_rejected(budget):
    with pytest.raises(SemanticError, match="TOKEN_BUDGET"):
        c.select_lines([], list, budget=budget)


@pytest.mark.parametrize("coordinates", [[{"file": 1, "line": 1}], [{"file": 0, "line": 1}] * 2])
def test_unknown_or_duplicate_baseline_seed_is_rejected(coordinates):
    groups = [{"hunk": 0, "eligible_lines": 1, "candidates": [(0, 0, "ok\n")]}]
    with pytest.raises(SemanticError, match="BASELINE_SEED_DRIFT"):
        c.select_lines(groups, list, baseline=coordinates)


def test_old_test_refused_before_git_or_tokenizer():
    with pytest.raises(SemanticError, match="OLD_TEST_FORBIDDEN"):
        c.transform({"member": {"partition": "test"}}, None, None)
    with pytest.raises(SemanticError, match="OLD_TEST_FORBIDDEN"):
        e.prepare_jobs([{"member": {"partition": "test"}}], None)


def test_real_git_exact_baseline_and_resealed_coordinate_text_blob_drift(work):
    from packages.semantic.git_inputs import extract

    dataset, source, _ = git_fixture(work)
    original = extract(dataset, source, work / "v1")
    row = next(r for r in artifacts.records(original) if r["member"]["partition"] == "train")
    preserved = artifacts.verify(original)
    with TemporaryDirectory(dir=work) as temporary:
        reader, _ = admission.source_reader(source, Path(temporary))
        old = admission.transform(row, reader, list)
        new = c.transform(old, reader, list)
        assert new["context"]["v2_reconstruction_exact"]
        assert new["roles"]["message"] == old["roles"]["message"]
        assert set(new["member"]) == {"canonical_repository", "partition", "commit_id"}
        for failure, message in [
            ("hunk", "HUNK_DRIFT"),
            ("text", "V2_RECONSTRUCTION_DRIFT"),
            ("blob", "BLOB_LENGTH_DRIFT"),
        ]:
            bad = json.loads(json.dumps(old))
            if failure == "hunk":
                bad["context"]["hunks"][0]["old"][0] += 1
            elif failure == "text":
                bad["roles"]["added"] = "invented context"
                bad["text_hash"] = content_hash(bad["roles"])
            else:
                bad["files"][0]["before_bytes"] += 1
            with pytest.raises(SemanticError, match=message):
                c.transform(bad, reader, list)
        assert not list(Path(temporary).iterdir())
    assert artifacts.verify(original) == preserved


@pytest.mark.parametrize("failure", ["parent", "checkpoint", "sources", "policy", "source"])
def test_changed_declaration_refused_before_source_access(work, monkeypatch, failure):
    parent = {"content_hash": "parent", "source": {"head": "a" * 40}}
    declared = {
        "version": "semantic-coverage-declaration-v1",
        "parent": "parent",
        "checkpoint": "weights",
        "sources": {"code": "fixed"},
        "policy": c.probe_policy(),
        "source": parent["source"],
    }
    declared[failure] = "changed"
    monkeypatch.setattr(c, "parent_rows", lambda _: (parent, []))
    monkeypatch.setattr(c, "safe_weights", lambda _: {"content_hash": "weights"})
    monkeypatch.setattr(artifacts, "verify", lambda *args: declared)
    monkeypatch.setattr(artifacts, "sources", lambda _: {"code": "fixed"})
    with pytest.raises(SemanticError, match="DECLARATION_DRIFT"):
        c.contexts(*[work / p for p in ("inputs", "declaration", "source", "checkpoint", "out")])


@pytest.mark.parametrize("failure", ["shape", "nonfinite"])
def test_invalid_forward_refused(failure):
    def infer(batch):
        return np.array([[np.nan, 1]]) if failure == "nonfinite" else np.ones((1, 1))

    with pytest.raises(SemanticError, match="FORWARD_INVALID"):
        e.aggregate([[0, 3, 2]], [(0, 0)], infer, 1, hidden=2)


@pytest.mark.parametrize("failure", ["deadline", "memory"])
def test_final_supervision_cannot_be_bypassed_by_terminal_exit(work, monkeypatch, failure):
    from scripts import semantic_coverage_probe as cli

    args = [
        "coverage",
        "select",
        "--inputs",
        str(work / "inputs"),
        "--checkpoint",
        str(work / "weights"),
        "--output",
        str(work / "out"),
        "--result",
        str(work / "result.json"),
        "--log",
        str(work / "log"),
    ]
    monkeypatch.setattr(cli.sys, "argv", args)
    monkeypatch.setattr(
        cli,
        "supervise",
        lambda *a, **k: {
            "seconds": 2701 if failure == "deadline" else 1,
            "peak_child_rss_bytes": policy.MAX_RSS + 1 if failure == "memory" else 1,
        },
    )
    with pytest.raises(SemanticError, match="FINAL_SUPERVISION_LIMIT"):
        cli.main()
    assert not (work / "result.json").exists()


def test_parent_accepts_ascii_result_after_windows_progress_bytes(work, monkeypatch):
    from scripts import semantic_coverage_probe as cli

    log = work / "child.log"
    target = work / "artifact"
    log.write_bytes(b"progress:\xa8\n" + json.dumps({"directory": str(target)}).encode() + b"\n")
    monkeypatch.setattr(
        cli.sys,
        "argv",
        [
            "coverage",
            "select",
            "--inputs",
            str(work / "inputs"),
            "--checkpoint",
            str(work / "weights"),
            "--output",
            str(work / "out"),
            "--result",
            str(work / "result.json"),
            "--log",
            str(log),
        ],
    )
    monkeypatch.setattr(
        cli,
        "supervise",
        lambda *a, **k: {"completed": True, "seconds": 1, "peak_child_rss_bytes": 1},
    )
    monkeypatch.setattr(artifacts, "verify", lambda *a: {"content_hash": "verified"})
    cli.main()
    assert assets.read_json(work / "result.json", lambda: None)["directory"] == str(target)


@pytest.mark.parametrize("body", [b"progress:\xa8\n", b'{"directory":"\xa8"}\n'])
def test_missing_or_non_ascii_result_is_not_accepted(work, body):
    from scripts.semantic_coverage_probe import read_result

    log = work / "invalid.log"
    log.write_bytes(body)
    with pytest.raises((SemanticError, UnicodeDecodeError)):
        read_result(log)


def repeat_fixture(work, name, *, changed=False):
    root = work / name
    root.mkdir()
    attempt = root / ".attempt"
    attempt.mkdir()
    values = np.zeros((96, 3076), dtype=np.float32)
    if changed:
        values[95, 3075] = 1
    np.save(attempt / "embeddings.npy", values, allow_pickle=False)
    assets.write_json(attempt / "rows.json", {"inert_comparison_fixture": True})
    assets.write_json(attempt / "coverage.json", {"inert_comparison_fixture": True})
    return artifacts.publish(
        attempt,
        root,
        {
            "version": "frozen-codebert-hunk-window-probe-v3",
            "shape": [96, 3076],
            "binding": {"inputs": "same-fixture", "sources": artifacts.sources(c.SOURCES)},
            "operation_id": name,
            "computed_new_forward": True,
            "model_frozen": True,
            "fixture_not_real_model_or_quality": True,
        },
        ["embeddings.npy", "rows.json", "coverage.json"],
    )


def test_repeat_requires_distinct_operations(work):
    first = repeat_fixture(work, "first")
    with pytest.raises(SemanticError, match="REPEAT_BINDING"):
        e.compare(first, first, work / "comparison")


def test_resealed_single_changed_vector_cannot_pass_repeat(work):
    first = repeat_fixture(work, "first")
    second = repeat_fixture(work, "second", changed=True)
    with pytest.raises(SemanticError, match="REPEAT_VALUES"):
        e.compare(first, second, work / "comparison")


def test_exact_repeat_is_not_all_artifact_nfr05_certification(work):
    first = repeat_fixture(work, "first")
    second = repeat_fixture(work, "second")
    target = e.compare(first, second, work / "comparison")
    manifest = artifacts.verify(target)
    result = assets.read_json(target / "comparison.json", lambda: None)
    assert result["all_vectors_exact"] and result["npy_bytes_exact"]
    assert not result["metadata_bytes_claimed_exact"]
    assert not result["nfr05_all_product_artifacts_certified"] and not manifest["candidate_release"]


def test_declaration_context_storage_roundtrip_keeps_members_and_licenses(work, monkeypatch):
    rows = population()
    for row in rows:
        row["context"]["roles"] = {role: {"unrepresented_hunks": 0} for role in c.CODE_ROLES}
    parent = work / "parents"
    parent.mkdir()
    stage = parent / ".stage"
    stage.mkdir()
    (stage / "records.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (stage / "licenses.json").write_bytes(b'{"fixture_license_binding":true}\n')
    receipt = {"repository": policy.REPOSITORY, "head": "a" * 40}
    inputs = artifacts.publish(
        stage,
        parent,
        {"version": "hunk-context-v2", "accepted": 660, "source": receipt},
        ["records.jsonl", "licenses.json"],
    )
    safe_root = work / "weights"
    safe_root.mkdir()
    stage = safe_root / ".stage"
    stage.mkdir()
    (stage / "inert").write_bytes(b"inert metadata fixture; no model loaded")
    safe = artifacts.publish(
        stage,
        safe_root,
        {
            "safe_format": "safetensors",
            "weights_only": True,
            "original": {"revision": policy.REVISION},
        },
        ["inert"],
    )
    declared = c.declare(inputs, safe, work / "declarations")
    expected = assets.read_json(declared / "selection.json", lambda: None)["members"]
    monkeypatch.setattr(admission, "source_reader", lambda *a: (None, receipt))

    def inert_transform(row, *args):
        metrics = dict.fromkeys(
            [
                "selected_tokens",
                "candidate_hunks",
                "represented_hunks",
                "unrepresented_hunks",
                "candidate_lines_before_cap",
                "candidate_lines_after_cap",
                "selected_hunk_line_incidences",
                "complete_eligible_hunks",
                "candidate_cap_omitted_lines",
            ],
            0,
        )
        audit = {role: metrics for role in c.CODE_ROLES}
        return {"member": c.identity(row), "context": {"baseline_roles": audit, "roles": audit}}

    monkeypatch.setattr(c, "transform", inert_transform)
    target = c.contexts(inputs, declared, work / "source", safe, work / "output", encode=list)
    actual = list(artifacts.records(target))
    assert [r["member"] for r in actual] == [
        {k: p[k] for k in ("canonical_repository", "partition", "commit_id")} for p in expected
    ]
    assert (target / "licenses.json").read_bytes() == (inputs / "licenses.json").read_bytes()
    manifest = artifacts.verify(target)
    assert manifest["accepted"] == 96 and not manifest["candidate_release"]
    assert not manifest["old_model_compatible"]
