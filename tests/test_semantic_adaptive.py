"""Independent content/cap/floor/window counterexamples with no model fitting."""

from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pytest

from packages.mining import public_assets as assets
from packages.mining.source_evidence import content_hash
from packages.semantic import adaptive as a
from packages.semantic import adaptive_encoder as e
from packages.semantic import admission, artifacts, coverage, encoder, policy
from packages.semantic.policy import SemanticError


def select(before, after, role, floor=()):
    files = [(before, after)]
    groups, hunks = admission.hunk_groups(files)
    classified = a.classify_groups(files, groups[role], hunks, role)
    return a.select_lines(classified, list(floor), list)


@pytest.mark.parametrize("floor_body", ["\n", "}\n", ";\n"])
def test_symbol_or_blank_floor_does_not_replace_available_code(floor_body):
    text, audit = select("old\n", floor_body + "real_code();\n", "added", [{"file": 0, "line": 1}])
    assert text == floor_body + "real_code();\n"
    assert audit["supported"] and audit["v2_selected_lines_preserved"]
    assert audit["hunks"][0]["selected_alphanumeric"] == 1
    assert audit["required_class_satisfied_hunks"] == 1


def test_changed_old_base_symbols_outrank_lexical_unchanged_context():
    text, audit = select("unchanged();\n}\nnext();\n", "unchanged();\n{\nnext();\n", "base")
    assert text == "}\n"
    assert audit["hunks"][0]["required_class"] == [1, 0, 1]
    assert audit["hunks"][0]["selected_changed_old"] == 1


@pytest.mark.parametrize("body", ["}\n", "\n", " \t\n", "\r\n"])
def test_legitimate_symbol_or_whitespace_change_remains_supported(body):
    text, audit = select("", body, "added")
    assert text == body and audit["supported"]
    assert audit["represented_hunks"] == audit["required_class_satisfied_hunks"] == 1


def test_insertion_base_context_is_explicitly_not_changed_old_code():
    text, audit = select("before();\nafter();\n", "before();\nnew();\nafter();\n", "base")
    assert text in {"before();\n", "after();\n"}
    assert audit["hunks"][0]["required_class"] == [0, 1, 1]
    assert audit["hunks"][0]["eligible_changed_old"] == 0


def test_real_content_after_32_candidate_cap_is_unsupported_not_blank_success():
    text, audit = select("", "\n" * 32 + "real_code();\n", "added")
    assert text == "" and not audit["supported"]
    assert audit["unsupported_reasons"] == [
        {"code": "required_content_outside_candidate_cap", "hunk": 0}
    ]
    assert audit["hunks"][0]["eligible_alphanumeric"] == 1
    assert audit["candidate_cap_omitted_incidences"] == 1


def test_floor_preserved_even_when_complete_selection_exceeds_budget():
    # Old line below252 must survive even when required content needs >4032.
    floor = [{"file": 0, "line": 1}]
    text, audit = select("", "}\n" + "x" * 4032 + "\n", "added", floor)
    assert text == "}\n" + "x" * 4032 + "\n"
    assert audit["v2_selected_lines_preserved"] and not audit["supported"]
    assert audit["unsupported_reasons"][0]["code"] == "content_token_budget"
    assert audit["selected_tokens"] == len(text) > 4032


def test_alphanumeric_unicode_and_crlf_physical_coordinates():
    text, audit = select("", "}\r\n变量();\r\n", "added", [{"file": 0, "line": 1}])
    assert text == "}\r\n变量();\r\n"
    assert audit["selected_coordinates"] == [{"file": 0, "line": 1}, {"file": 0, "line": 2}]


@pytest.mark.parametrize("floor", [[{"file": 1, "line": 1}], [{"file": 0, "line": 1}] * 2])
def test_unknown_or_duplicate_floor_rejected(floor):
    with pytest.raises(SemanticError, match="FLOOR_DRIFT"):
        select("", "code\n", "added", floor)


def test_no_extra_filling_and_deterministic_cost_ties():
    text, audit = select("", "aa\nbb\ncc\n", "added")
    assert text == "aa\n" and len(audit["selected_coordinates"]) == 1


def test_actual_joined_cost_not_additive_local_line_cost():
    files = [("", "a\n"), ("", "b\n")]
    groups, hunks = admission.hunk_groups(files)
    groups = a.classify_groups(files, groups["added"], hunks, "added")

    def tokenize(value):
        return range(5000 if "a\n\nb\n" == value else len(value))

    text, audit = a.select_lines(groups, [], tokenize)
    assert text == "a\n\nb\n" and not audit["supported"]
    assert audit["selected_tokens"] == 5000


def test_shared_base_incidence_does_not_duplicate_selected_whole_line():
    group = {
        "hunk": 0,
        "eligible_lines": 1,
        "candidates": [(0, 0, "a\n")],
        "required_candidates": [(0, 0)],
        "required_class": [1, 1, 1],
        "eligible_nonblank": 1,
        "eligible_alphanumeric": 1,
        "eligible_changed_old": 1,
        "candidate_classes": [[1, 1, 1]],
    }
    text, audit = a.select_lines([group, group | {"hunk": 1}], [], list)
    assert text == "a\n" and len(audit["selected_coordinates"]) == 1
    assert audit["required_class_satisfied_hunks"] == 2
    assert sum(h["selected_incidences"] for h in audit["hunks"]) == 2


@pytest.mark.parametrize("length", [0, 1, 125, 126, 127, 1008, 1009, 4031, 4032])
def test_new_windows_cover_every_token_without_touching_old_limit(length):
    ids = list(range(length))
    chunks, audit = e.windows(ids)
    assert [token for chunk in chunks for token in chunk] == ids
    assert len(chunks) <= 32 and all(len(c) <= 126 for c in chunks)
    assert audit["covered_tokens"] == length and audit["omitted_tokens"] == 0
    with pytest.raises(SemanticError, match="WINDOW_POLICY"):
        encoder.windows(ids, count=32)


def test_new_window_budget_not_truncated():
    with pytest.raises(SemanticError, match="TOKEN_BUDGET"):
        e.windows(list(range(4033)))


def supported_rows():
    rows = []
    for i in range(96):
        member = {
            "canonical_repository": policy.REPOSITORY,
            "partition": "train" if i < 64 else "validation",
            "commit_id": f"{i + 1:040x}",
        }
        roles = {"message": "m", "added": "a" * 1009, "deleted": "", "base": "b"}
        rows.append(
            {
                "member": member,
                "roles": roles,
                "text_hash": content_hash(roles),
                "context": {
                    "version": a.VERSION,
                    "supported": True,
                    "message": {"omitted_tokens": 0},
                    "roles": {
                        r: {
                            "supported": True,
                            "unsupported_reasons": [],
                            "selected_tokens": len(roles[r]),
                        }
                        for r in coverage.CODE_ROLES
                    },
                },
            }
        )
    return rows


def test_jobs_exact_locations_role_masks_and_no_token_omissions():
    jobs, locations, audit = e.prepare_jobs(supported_rows(), list)
    assert len(jobs) == 96 * 11  # message1, added9, deleted0, base1
    assert len(locations) == len(jobs) and len(audit) == 96
    for i in range(96):
        reconstructed = [
            token
            for job, loc in zip(jobs, locations, strict=True)
            if loc == (i, 1)
            for token in job[1:-1]
        ]
        assert reconstructed == list("a" * 1009)
    assert all(job[0] == 0 and job[-1] == 2 and 2 < len(job) <= 128 for job in jobs)


@pytest.mark.parametrize(
    "failure", ["unsupported", "reason", "message", "test", "duplicate", "missing"]
)
def test_population_rejected_without_calling_tokenizer(failure):
    rows = supported_rows()
    if failure == "unsupported":
        rows[-1]["context"]["supported"] = False
    elif failure == "reason":
        rows[-1]["context"]["roles"]["base"]["unsupported_reasons"] = [{"code": "bad"}]
    elif failure == "message":
        rows[-1]["context"]["message"]["omitted_tokens"] = 1
    elif failure == "test":
        rows[-1]["member"]["partition"] = "test"
    elif failure == "duplicate":
        rows[-1]["member"] = rows[-2]["member"]
    else:
        rows.pop()
    with pytest.raises(SemanticError):
        e.prepare_jobs(rows, lambda _: pytest.fail("must reject before tokenization"))


def test_unsupported_manifest_rejected_before_torch_or_weights(monkeypatch):
    monkeypatch.setattr(
        artifacts,
        "verify",
        lambda *_: {"version": a.VERSION, "accepted": 96, "supported": 95, "all_supported": False},
    )
    monkeypatch.setattr(
        coverage, "safe_weights", lambda *_: pytest.fail("unsupported must reject before weights")
    )
    with TemporaryDirectory(dir=assets.ROOT / "runtime/temp") as name:
        work = Path(name)
        with pytest.raises(SemanticError, match="ENCODING_BINDING"):
            e.encode(work / "input", work / "weights", work / "output")
        assert not (work / "output").exists()


@pytest.mark.parametrize("failure", ["source", "input", "parent", "weights"])
def test_declaration_start_end_guards_reject_drift(monkeypatch, failure):
    changed = [False]
    sources = {p: "0" * 64 for p in a.SOURCES}
    manifest = {"content_hash": "1" * 64, "checkpoint": "3" * 64}
    parent = {"content_hash": "2" * 64}
    weights = {"content_hash": "3" * 64}

    def load(directory):
        changed[0] = True
        return manifest, directory.parent / "parent", parent, [], supported_rows()

    def verify(path, *_):
        value = parent if path.name == "parent" else manifest
        return value | (
            {"drift": True}
            if failure == ("parent" if path.name == "parent" else "input") and changed[0]
            else {}
        )

    monkeypatch.setattr(a, "load_inputs", load)
    monkeypatch.setattr(
        artifacts,
        "sources",
        lambda _: sources | ({"new": "4" * 64} if failure == "source" and changed[0] else {}),
    )
    monkeypatch.setattr(artifacts, "verify", verify)
    calls = [0]

    def safe_weights(_):
        calls[0] += 1
        return weights | ({"drift": True} if failure == "weights" and calls[0] > 1 else {})

    monkeypatch.setattr(coverage, "safe_weights", safe_weights)
    with TemporaryDirectory(dir=assets.ROOT / "runtime/temp") as name:
        work = Path(name)
        with pytest.raises(SemanticError, match="DECLARATION_DRIFT"):
            a.declare(work / "input", work / "weights", work / "output")
        assert all(p.name.startswith(".declare-") for p in (work / "output").iterdir())


def test_token_audit_drift_rejected():
    rows = supported_rows()
    rows[-1]["context"]["roles"]["added"]["selected_tokens"] -= 1
    with pytest.raises(SemanticError, match="TOKEN_DRIFT"):
        e.prepare_jobs(rows, list)


def test_message_not_silently_truncated_after_sealed_audit():
    rows = supported_rows()
    rows[0]["roles"]["message"] = "x" * 253
    rows[0]["text_hash"] = content_hash(rows[0]["roles"])
    with pytest.raises(SemanticError, match="TOKENS_OMITTED"):
        e.prepare_jobs(rows, list)


def test_transform_reconstructs_both_versions_and_rejects_old_drift():
    before, after = "old();\n", "}\nnew_code();\n"
    blobs = {}
    from packages.semantic.admission_git import object_hash

    for text in (before, after):
        blobs[object_hash("blob", text.encode())] = text.encode()

    class Reader:
        deadline = 0

        def command(self, arguments, **_):
            assert arguments[:2] == ["cat-file", "blob"]
            return blobs[arguments[-1]]

    reader = Reader()
    groups, hunks = admission.hunk_groups([(before, after)])
    roles, audits = {"message": "message"}, {}
    for role in coverage.CODE_ROLES:
        roles[role], audits[role] = admission.select_lines(groups[role], list)
    row = {
        "member": {
            "canonical_repository": policy.REPOSITORY,
            "partition": "train",
            "commit_id": "a" * 40,
        },
        "files": [
            {
                "old_blob": object_hash("blob", before.encode()),
                "new_blob": object_hash("blob", after.encode()),
                "before_bytes": len(before),
                "after_bytes": len(after),
            }
        ],
        "roles": roles,
        "text_hash": content_hash(roles),
        "context": {"hunks": hunks, "roles": audits},
    }
    previous = coverage.transform(row, reader, list)
    result = a.transform(row, previous, reader, list)
    assert result["context"]["supported"]
    assert all(result["context"][k] for k in ("v2_reconstruction_exact", "v3_reconstruction_exact"))
    assert result["roles"]["added"] == after
    previous["roles"]["added"] = "drift"
    with pytest.raises(SemanticError, match="V3_RECONSTRUCTION_DRIFT"):
        a.transform(row, previous, reader, list)


def repeat_fixture(work, name, *, change=None):
    root = work / name
    root.mkdir()
    attempt = root / ".stage"
    attempt.mkdir()
    values = np.zeros((96, 3076), dtype=np.float32)
    if change == "value":
        values[95, 1] = 1
    np.save(attempt / "embeddings.npy", values, allow_pickle=False)
    assets.write_json(attempt / "coverage.json", {"fixture": change == "coverage"})
    assets.write_json(attempt / "rows.json", {"fixture": change == "rows"})
    return artifacts.publish(
        attempt,
        root,
        {
            "version": e.VERSION,
            "shape": [96, 3076],
            "binding": {"sources": artifacts.sources(a.SOURCES)},
            "operation_id": name,
            "process_id": 1 if name == "first" else 2,
            "computed_new_forward": True,
            "model_frozen": True,
            "first_batch_repeat_exact": True,
            "inert_fixture_only": True,
        },
        ["embeddings.npy", "coverage.json", "rows.json"],
    )


@pytest.mark.parametrize("change", ["value", "coverage", "rows"])
def test_repeat_rejects_any_resealed_result_file_difference(change):
    with TemporaryDirectory(dir=assets.ROOT / "runtime/temp") as name:
        work = Path(name)
        first, second = repeat_fixture(work, "first"), repeat_fixture(work, "second", change=change)
        with pytest.raises(SemanticError, match="REPEAT_VALUES"):
            e.compare(first, second, work / "output")


def test_complete_comparison_storage_roundtrip_and_same_run_rejection():
    with TemporaryDirectory(dir=assets.ROOT / "runtime/temp") as name:
        work = Path(name)
        first, second = repeat_fixture(work, "first"), repeat_fixture(work, "second")
        target = e.compare(first, second, work / "output")
        assert not artifacts.verify(target)["candidate_release"]
        result = assets.read_json(target / "comparison.json", lambda: None)
        assert result["all_files_bytes_exact"] and result["new_full_forward_both"]
        assert not result["arbitrary_serving_batch_invariance_certified"]
        with pytest.raises(SemanticError, match="REPEAT_BINDING"):
            e.compare(first, first, work / "invalid")


@pytest.mark.parametrize("failure", ["deadline", "memory", "incomplete"])
def test_cli_final_limits_before_result_publication(monkeypatch, failure):
    from scripts import semantic_adaptive_probe as cli

    with TemporaryDirectory(dir=assets.ROOT / "runtime/temp") as name:
        work = Path(name)
        monkeypatch.setattr(
            cli.sys,
            "argv",
            [
                "probe",
                "declare",
                "--inputs",
                str(work / "input"),
                "--checkpoint",
                str(work / "weights"),
                "--output",
                str(work / "out"),
                "--result",
                str(work / "result.json"),
                "--log",
                str(work / "child.log"),
            ],
        )
        monkeypatch.setattr(
            cli,
            "supervise",
            lambda *_args, **_kwargs: {
                "completed": failure != "incomplete",
                "seconds": 2701 if failure == "deadline" else 1,
                "peak_child_rss_bytes": policy.MAX_RSS + 1 if failure == "memory" else 1,
            },
        )
        with pytest.raises(SemanticError, match="FINAL_SUPERVISION_LIMIT"):
            cli.main()
        assert not (work / "result.json").exists()


@pytest.mark.parametrize("failure", ["source", "inputs", "parent", "declaration", "weights"])
def test_contexts_mid_transform_drift_cannot_publish(monkeypatch, failure):
    # Only transform is inert; actual storage/final verification must reject the drift.
    with TemporaryDirectory(dir=assets.ROOT / "runtime/temp") as name:
        work = Path(name)
        inputs, parent_dir, declared_dir, weights_dir = [
            work / p for p in ("inputs", "parent", "declaration", "weights")
        ]
        inputs.mkdir()
        (inputs / "licenses.json").write_bytes(b'{"fixture":true}')
        declared_dir.mkdir()
        rows = supported_rows()
        for row in rows:
            for audit in row["context"]["roles"].values():
                audit["v2_selected_lines_preserved"] = True
        assets.write_json(declared_dir / "selection.json", {"members": [r["member"] for r in rows]})
        sources = artifacts.sources(a.SOURCES)
        receipt = {"repository": policy.REPOSITORY}
        manifest = {"content_hash": "1" * 64, "source": receipt}
        parent = {"content_hash": "2" * 64, "source": receipt}
        weights = {"content_hash": "3" * 64}
        declared = {
            "version": "adaptive-context-declaration-v1",
            "accepted": 96,
            "sources": sources,
            "inputs": manifest["content_hash"],
            "parent": parent["content_hash"],
            "checkpoint": weights["content_hash"],
            "policy": a.probe_policy(),
        }
        changed = [False]
        monkeypatch.setattr(a, "load_inputs", lambda _: (manifest, parent_dir, parent, rows, rows))
        monkeypatch.setattr(
            artifacts,
            "sources",
            lambda _: sources | ({"drift": "f" * 64} if failure == "source" and changed[0] else {}),
        )
        lookup = {inputs: manifest, parent_dir: parent, declared_dir: declared}

        def verify(path, *_):
            return lookup[path] | ({"drift": True} if changed[0] and path.name == failure else {})

        monkeypatch.setattr(artifacts, "verify", verify)
        monkeypatch.setattr(
            coverage,
            "safe_weights",
            lambda _: weights | ({"drift": True} if failure == "weights" and changed[0] else {}),
        )
        monkeypatch.setattr(admission, "source_reader", lambda *_: (None, receipt))

        def transform(row, *_):
            changed[0] = True
            return row

        monkeypatch.setattr(a, "transform", transform)
        with pytest.raises(SemanticError, match="CONTEXT_DRIFT"):
            a.contexts(
                inputs, declared_dir, work / "source", weights_dir, work / "output", tokenize=list
            )
        assert not list((work / "output").glob("*/manifest.json"))
