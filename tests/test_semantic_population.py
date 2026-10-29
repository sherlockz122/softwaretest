"""Independent population, control, unsupported, merge and repeat counterexamples."""

import copy
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pytest

from packages.mining import public_assets as assets
from packages.mining.source_evidence import content_hash
from packages.semantic import adaptive, admission, artifacts, coverage, encoder, policy
from packages.semantic import population as p
from packages.semantic import population_encoder as e
from packages.semantic.admission_git import object_hash
from packages.semantic.policy import SemanticError


@pytest.fixture
def work():
    with TemporaryDirectory(dir=assets.ROOT / "runtime/temp") as name:
        yield Path(name)


def population_rows():
    rows = []
    for part, total in (("train", 488), ("validation", 172)):
        counts = [
            n
            for n, quota in zip((1, 2, 9, 33, 129), coverage.QUOTAS[part], strict=True)
            for _ in range(quota)
        ]
        counts.extend([1] * (total - len(counts)))
        for n in counts:
            rows.append(
                {
                    "member": {
                        "canonical_repository": policy.REPOSITORY,
                        "partition": part,
                        "commit_id": f"{len(rows) + 1:040x}",
                    },
                    "files": [{}],
                    "text_hash": "v2-fixture",
                    "context": {
                        "hunks": [{}] * n,
                        "roles": {k: {"selected_coordinates": []} for k in coverage.CODE_ROLES},
                    },
                }
            )
    return rows


def supported_rows():
    rows = []
    for i, old in enumerate(population_rows()):
        roles = {"message": "m", "added": "a" * 253, "deleted": "", "base": "b"}
        audit = {
            k: {
                "supported": True,
                "unsupported_reasons": [],
                "v2_selected_lines_preserved": True,
                "selected_tokens": len(roles[k]),
                "selected_coordinates": [],
            }
            for k in coverage.CODE_ROLES
        }
        rows.append(
            {
                "population_index": i,
                "member": old["member"],
                "roles": roles,
                "text_hash": content_hash(roles),
                "code_hash": content_hash({k: v for k, v in roles.items() if k != "message"}),
                "context": {
                    "version": p.VERSION,
                    "supported": True,
                    "roles": audit,
                    "v2_reconstruction_exact": True,
                    "v4_control_exact": None,
                    "v2_text_hash": old["text_hash"],
                    "message": encoder.windows(list("m"))[1],
                },
            }
        )
    return rows


def test_exact_original_population_and_actual_unpadded_tail():
    rows = population_rows()
    members = p.validate_members(rows)
    assert members == [coverage.identity(r) for r in rows]
    assert p.SLICES == (
        (0, 96),
        (96, 192),
        (192, 288),
        (288, 384),
        (384, 480),
        (480, 576),
        (576, 660),
    )
    assert [b - a for a, b in p.SLICES] == [96] * 6 + [84]
    assert [i for a, b in p.SLICES for i in range(a, b)] == list(range(660))


@pytest.mark.parametrize(
    "fault",
    ["zero", "upper", "short", "other", "duplicate", "test", "split", "missing", "files", "hunks"],
)
def test_complete_population_identity_split_and_boundaries(fault):
    rows = population_rows()
    if fault in {"zero", "upper", "short"}:
        rows[-1]["member"]["commit_id"] = {"zero": "0" * 40, "upper": "A" * 40, "short": "a" * 39}[
            fault
        ]
    elif fault == "other":
        rows[-1]["member"]["canonical_repository"] = "foreign/repo"
    elif fault == "duplicate":
        rows[-1]["member"] = rows[0]["member"]
    elif fault in {"test", "split"}:
        rows[-1]["member"]["partition"] = "test" if fault == "test" else "train"
    elif fault == "missing":
        rows.pop()
    elif fault == "files":
        rows[-1]["files"] = [{}] * 21
    else:
        rows[-1]["context"]["hunks"] = [{}] * 257
    with pytest.raises(SemanticError):
        p.validate_members(rows)


@pytest.mark.parametrize("index", [-1, 7, True, "6", 6.0])
def test_slice_index_rejects_implicit_coercion(index):
    with pytest.raises(SemanticError, match="CHUNK_INDEX"):
        p.slice_bounds(index)


def original_fixture():
    before, after = "old();\n", "}\nnew_code();\n"
    blobs = {object_hash("blob", text.encode()): text.encode() for text in (before, after)}

    class Reader:
        deadline = 0
        calls = 0

        def command(self, arguments, **_):
            self.calls += 1
            return blobs[arguments[-1]]

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
    return row, Reader


def test_original96_full_controls_recomputed_using_each_blob_once():
    row, Reader = original_fixture()
    old3 = coverage.transform(row, Reader(), list)
    old4 = adaptive.transform(row, old3, Reader(), list)
    reader = Reader()
    value = p.transform(row, (old3, old4), reader, list, 612)
    assert reader.calls == 2
    assert value["population_index"] == 612
    assert value["context"]["v4_control_exact"] is True
    assert value["roles"] == old4["roles"] and value["context"]["roles"] == old4["context"]["roles"]
    assert p.transform(row, None, Reader(), list, 2)["context"]["v3_reconstruction_exact"] is None


@pytest.mark.parametrize("fault", ["v2text", "v2audit", "hunks", "v3", "v4text", "v4audit"])
def test_original_versions_control_drift_cannot_publish(fault):
    row, Reader = original_fixture()
    old3 = coverage.transform(row, Reader(), list)
    old4 = adaptive.transform(row, old3, Reader(), list)
    if fault == "v2text":
        row["roles"]["added"] = "drift"
    elif fault == "v2audit":
        row["context"]["roles"]["added"]["selected_coordinates"] = []
    elif fault == "hunks":
        row["context"]["hunks"][0]["new"][0] += 1
    elif fault == "v3":
        old3["context"]["baseline_roles"]["base"]["selected_tokens"] += 1
    elif fault == "v4text":
        old4["roles"]["added"] = "drift"
    else:
        old4["context"]["roles"]["base"]["hunks"][0]["selected_changed_old"] += 1
    with pytest.raises(SemanticError):
        p.transform(row, (old3, old4), Reader(), list, 0)


def test_record_limit_fails_instead_of_dropping_member(monkeypatch):
    row, Reader = original_fixture()
    monkeypatch.setattr(policy, "MAX_RECORD", 10)
    with pytest.raises(SemanticError, match="RECORD_LIMIT"):
        p.transform(row, None, Reader(), list, 0)


@pytest.mark.parametrize(
    "fault", ["order", "index", "text", "codehash", "missing", "support", "version"]
)
def test_final_population_row_drift(fault):
    rows = supported_rows()
    members = [r["member"] for r in rows]
    if fault == "order":
        rows[0], rows[1] = rows[1], rows[0]
    elif fault == "index":
        rows[-1]["population_index"] = 0
    elif fault == "text":
        rows[-1]["roles"]["base"] = "foreign"
    elif fault == "codehash":
        rows[-1]["code_hash"] = "0" * 64
    elif fault == "missing":
        rows.pop()
    elif fault == "support":
        rows[-1]["context"]["roles"]["base"]["unsupported_reasons"] = [{"code": "cap"}]
    else:
        rows[-1]["context"]["version"] = adaptive.VERSION
    with pytest.raises(SemanticError):
        p.validate_rows(rows, members)


def test_unsupported_retained_and_all660_checked_before_any_forward(monkeypatch, work):
    rows = supported_rows()
    rows[-1]["context"]["supported"] = False
    rows[-1]["context"]["roles"]["added"].update(
        supported=False, unsupported_reasons=[{"code": "cap"}]
    )
    members = [r["member"] for r in rows]
    p.validate_rows(rows, members)
    audit = p.audit_rows(rows)
    assert audit["members"] == 660 and audit["supported"] == 659
    assert audit["unsupported_members"][0]["population_index"] == 659

    def load(*_, require_supported=False):
        p.validate_rows(rows, members, require_supported=require_supported)
        pytest.fail("unsupported in last chunk must stop even first chunk")

    monkeypatch.setattr(p, "load_contexts", load)
    with pytest.raises(SemanticError, match="UNSUPPORTED"):
        e.encode(work / "inputs", work / "output", 0)
    assert not (work / "output").exists()


def test_actual84_tail_jobs_locations_and_all_selected_tokens():
    rows = supported_rows()[576:660]
    jobs, locations, audits = e.prepare_jobs(rows, list)
    assert len(audits) == 84 and audits[0]["population_index"] == 576
    assert len(jobs) == 84 * 5  # message1, added3, deleted0, base1
    for i in range(84):
        assert [
            t for job, loc in zip(jobs, locations, strict=True) if loc == (i, 1) for t in job[1:-1]
        ] == list("a" * 253)
    assert max(x[0] for x in locations) == 83 and len(audits) == 84


@pytest.mark.parametrize("fault", ["token", "message", "overbudget"])
def test_jobs_reject_token_drift_and_omissions(fault):
    rows = supported_rows()[576:660]
    if fault == "token":
        rows[-1]["context"]["roles"]["added"]["selected_tokens"] -= 1
    elif fault == "message":
        rows[-1]["roles"]["message"] = "m" * 253
    else:
        rows[-1]["roles"]["added"] = "a" * 4033
    with pytest.raises(SemanticError):
        e.prepare_jobs(rows, list)


def manifests(version="v", declaration="d"):
    return [
        {
            "version": version,
            "accepted": b - a,
            "index": i,
            "range": [a, b],
            "declaration": declaration,
        }
        for i, (a, b) in enumerate(p.SLICES)
    ]


@pytest.mark.parametrize(
    "fault", ["missing", "duplicate", "reorder", "foreign", "tail", "range", "version"]
)
def test_strict_seven_chunk_set(fault, monkeypatch, work):
    parts = [work / str(i) for i in range(7)]
    records = manifests()
    if fault == "missing":
        parts.pop()
    elif fault == "duplicate":
        parts[-1] = parts[0]
    elif fault == "reorder":
        records[0], records[1] = records[1], records[0]
    elif fault == "foreign":
        records[-1]["declaration"] = "foreign"
    elif fault == "tail":
        records[-1]["accepted"] = 96
    elif fault == "range":
        records[-1]["range"] = [575, 659]
    else:
        records[-1]["version"] = "old"
    monkeypatch.setattr(artifacts, "verify", lambda path, *_: records[int(path.name)])
    with pytest.raises(SemanticError):
        p.chunk_manifests(parts, "v", "d")


def payload(work, rows, fault=None):
    work.mkdir()
    values = np.ones((len(rows), 3076), np.float32)
    values[:, 1536:2304] = 0  # deleted is empty
    values[:, -4:] = [1, 1, 0, 1]
    if fault == "nan":
        values[-1, 0] = np.nan
    elif fault == "shape":
        values = values[:-1]
    elif fault == "dtype":
        values = values.astype(np.float64)
    elif fault == "mask":
        values[-1, -1] = 0
    elif fault == "empty":
        values[-1, 1536] = 1
    np.save(work / "embeddings.npy", values, allow_pickle=False)
    rowdata, audits = e.row_records(rows), e.expected_audits(rows)
    if fault == "rows":
        rowdata[-1]["population_index"] = 0
    elif fault == "coverage":
        audits[-1]["roles"]["added"]["covered_tokens"] -= 1
    assets.write_json(work / "rows.json", {"records": rowdata})
    assets.write_json(work / "coverage.json", {"records": audits})
    return values


@pytest.mark.parametrize("fault", ["nan", "shape", "dtype", "mask", "empty", "rows", "coverage"])
def test_tail_payload_nonfinite_shape_identity_coverage_and_masks_rejected(work, fault):
    rows = supported_rows()[576:660]
    payload(work / "payload", rows, fault)
    with pytest.raises(SemanticError):
        e.read_payload(work / "payload", rows)


@pytest.mark.parametrize("fault", ["source", "parent", "member", "policy", "weights"])
def test_declaration_rechecks_frozen_bundle_before_publication(monkeypatch, work, fault):
    rows = population_rows()
    binding = {"sources": "a", "parents": ["x"], "members": "a", "policy": "a", "weights": "a"}
    monkeypatch.setattr(p, "load_bundle", lambda *_: (binding, rows, {}))
    altered = copy.deepcopy(binding)
    altered[{"source": "sources", "parent": "parents", "member": "members"}.get(fault, fault)] = (
        "drift"
    )
    monkeypatch.setattr(p, "reload_bundle", lambda *_: (altered, rows, {}))
    with pytest.raises(SemanticError, match="INPUT_DRIFT"):
        p.declare(work / "v2", work / "v3", work / "v4", work / "weights", work / "out")
    assert len(list((work / "out").glob(".declare-*"))) == 1
    assert not list((work / "out").glob("*/manifest.json"))


@pytest.mark.parametrize("fault", ["deadline", "memory", "incomplete"])
def test_cli_supervision_limit_before_result(monkeypatch, work, fault):
    from scripts import semantic_population_probe as cli

    args = [
        "probe",
        "encode",
        "--inputs",
        str(work / "in"),
        "--chunk-index",
        "6",
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
        lambda *_, **__: {
            "completed": fault != "incomplete",
            "seconds": policy.MAX_SECONDS + (fault == "deadline"),
            "peak_child_rss_bytes": policy.MAX_RSS + (fault == "memory"),
        },
    )
    with pytest.raises(SemanticError, match="FINAL_SUPERVISION_LIMIT"):
        cli.main()
    assert not (work / "result.json").exists()


def encoding_fixture(work, name, rows, original, *, fault=None):
    root = work / name
    root.mkdir()
    parts = []
    binding = {
        "inputs": original["content_hash"],
        "declaration": original["declaration"],
        "checkpoint": original["binding"]["parents"][3],
        "sources": artifacts.sources(p.SOURCES),
        "execution": e.protocol(),
        "dependencies": {
            k: "fixture" for k in ("torch", "transformers", "tokenizers", "safetensors", "numpy")
        },
    }
    for i, (a, b) in enumerate(p.SLICES):
        staging = root / (".part-" + str(i))
        payload(staging, rows[a:b])
        values = np.load(staging / "embeddings.npy", allow_pickle=False)
        values[:, 0] = np.arange(a, b)
        if fault == "value" and i == 6:
            values[-1, 1] += 1
        np.save(staging / "embeddings.npy", values, allow_pickle=False)
        m = {
            "version": e.VERSION + "-chunk",
            "accepted": b - a,
            "index": i,
            "range": [a, b],
            "declaration": original["declaration"],
            "shape": [b - a, 3076],
            "binding": copy.deepcopy(binding),
            "operation_id": f"{i + (20 if name == 'second' else 1):032x}",
            "process_id": i + (200 if name == "second" else 100),
            "computed_new_forward": True,
            "model_frozen": True,
            "first_batch_repeat_exact": True,
            "windows": sum(
                x["roles"][k]["all_windows"]
                for x in e.expected_audits(rows[a:b])
                for k in policy.ROLES
            ),
            "inert_fixture_only": True,
        }
        if i == 6:
            if fault == "sources":
                m["binding"]["sources"] = {}
            elif fault == "execution":
                m["binding"]["execution"]["batch"] = 1
            elif fault == "weights":
                m["binding"]["checkpoint"] = "foreign"
            elif fault == "dependencies":
                m["binding"]["dependencies"]["numpy"] = "foreign"
            elif fault == "forward":
                m["computed_new_forward"] = False
            elif fault == "reused":
                m["operation_id"] = f"{1:032x}"
            elif fault == "windows":
                m["windows"] -= 1
            elif fault == "shape":
                m["shape"] = [96, 3076]
        parts.append(artifacts.publish(staging, root, m, e.FILES))
    return parts


def encoding_inputs(monkeypatch):
    rows = supported_rows()
    original = {
        "content_hash": "ctx",
        "declaration": "decl",
        "binding": {"parents": ["a", "b", "c", "weights"]},
    }
    monkeypatch.setattr(p, "load_contexts", lambda *_, **__: (original, rows))
    return original, rows


def test_full_merge_and_repeat_really_retain660_and84_tail(monkeypatch, work):
    original, rows = encoding_inputs(monkeypatch)
    merged = []
    for name in ("first", "second"):
        parts = encoding_fixture(work, name, rows, original)
        merged.append(e.merge(work / "input", parts, work / (name + "-merged")))
    values = e.read_payload(merged[0], rows)
    np.testing.assert_array_equal(values[:, 0], np.arange(660))
    assert values.shape == (660, 3076)
    proof = e.compare(*merged, work / "comparison")
    assert artifacts.verify(proof)["accepted"] == 660
    assert assets.read_json(proof / "comparison.json", lambda: None)[
        "all_three_payload_files_bytes_exact"
    ]
    with pytest.raises(SemanticError, match="REPEAT_BINDING"):
        e.compare(merged[0], merged[0], work / "invalid")


@pytest.mark.parametrize(
    "fault",
    ["sources", "execution", "weights", "dependencies", "forward", "reused", "windows", "shape"],
)
def test_merge_rejects_resealed_tail_protocol_and_forward_drift(monkeypatch, work, fault):
    original, rows = encoding_inputs(monkeypatch)
    parts = encoding_fixture(work, "first", rows, original, fault=fault)
    with pytest.raises(SemanticError):
        e.merge(work / "input", parts, work / "invalid")
    assert not (work / "invalid").exists()


def test_complete_repeat_checks_final_real_tail_vector(monkeypatch, work):
    original, rows = encoding_inputs(monkeypatch)
    first = e.merge(work / "input", encoding_fixture(work, "first", rows, original), work / "m1")
    second = e.merge(
        work / "input", encoding_fixture(work, "second", rows, original, fault="value"), work / "m2"
    )
    with pytest.raises(SemanticError, match="REPEAT_VALUES"):
        e.compare(first, second, work / "invalid")


@pytest.mark.parametrize(
    "fault", ["missing", "duplicate", "order", "identity", "support", "binding", "enddrift"]
)
def test_context_merge_strict_identity_support_and_entry_exit_binding(monkeypatch, work, fault):
    rows, originals = supported_rows(), population_rows()
    binding = {"directories": {"v2": work.relative_to(assets.ROOT).as_posix()}}
    declaration = {"content_hash": "decl", "binding": binding}
    (work / "licenses.json").write_text("{}", encoding="utf-8")
    calls = [0]

    def load(*_):
        calls[0] += 1
        return (
            (
                declaration | {"drift": True}
                if fault == "enddrift" and calls[0] > 1
                else declaration
            ),
            originals,
            {},
        )

    monkeypatch.setattr(p, "load_declaration", load)
    root = work / "chunks"
    root.mkdir()
    parts = []
    for i, (a, b) in enumerate(p.SLICES):
        staging = root / (".part-" + str(i))
        staging.mkdir()
        partrows = copy.deepcopy(rows[a:b])
        if fault == "identity" and i == 6:
            partrows[-1]["member"] = partrows[0]["member"]
        p.write_contexts(staging, partrows, work / "licenses.json")
        parts.append(
            artifacts.publish(
                staging,
                root,
                {
                    "version": p.VERSION + "-chunk",
                    "index": i,
                    "range": [a, b],
                    "accepted": b - a,
                    "declaration": "decl",
                    "binding": binding if fault != "binding" or i != 6 else {},
                    "supported": b - a - (fault == "support" and i == 6),
                },
                ["records.jsonl", "audit.json", "licenses.json"],
            )
        )
    if fault == "missing":
        parts.pop()
    elif fault == "duplicate":
        parts[-1] = parts[0]
    elif fault == "order":
        parts[0], parts[1] = parts[1], parts[0]
    with pytest.raises(SemanticError):
        p.merge_contexts(work / "decl", parts, work / "invalid")


def test_context_merge_success_keeps_original_order_and_all_rows(monkeypatch, work):
    rows, originals = supported_rows(), population_rows()
    (work / "licenses.json").write_text("{}", encoding="utf-8")
    binding = {"directories": {"v2": work.relative_to(assets.ROOT).as_posix()}}
    declaration = {"content_hash": "decl", "binding": binding}
    monkeypatch.setattr(p, "load_declaration", lambda *_: (declaration, originals, {}))
    root = work / "chunks"
    root.mkdir()
    parts = []
    for i, (a, b) in enumerate(p.SLICES):
        staging = root / (".part-" + str(i))
        staging.mkdir()
        p.write_contexts(staging, rows[a:b], work / "licenses.json")
        parts.append(
            artifacts.publish(
                staging,
                root,
                {
                    "version": p.VERSION + "-chunk",
                    "index": i,
                    "range": [a, b],
                    "accepted": b - a,
                    "declaration": "decl",
                    "binding": binding,
                    "supported": b - a,
                },
                ["records.jsonl", "audit.json", "licenses.json"],
            )
        )
    result = p.merge_contexts(work / "decl", parts, work / "merged")
    assert list(artifacts.records(result)) == rows
    assert artifacts.verify(result)["all_supported"] is True


@pytest.mark.parametrize(
    "fault", ["parent", "floor", "duplicate", "v3", "v4", "v4audit", "fictional"]
)
def test_control_and_old_floor_rechecked_when_loading_merged_rows(fault):
    old, Reader = original_fixture()
    old3 = coverage.transform(old, Reader(), list)
    old4 = adaptive.transform(old, old3, Reader(), list)
    row = p.transform(old, (old3, old4), Reader(), list, 0)
    if fault == "parent":
        row["context"]["v2_text_hash"] = "foreign"
    elif fault == "floor":
        row["context"]["roles"]["added"]["selected_coordinates"] = []
    elif fault == "duplicate":
        row["context"]["roles"]["added"]["selected_coordinates"] *= 2
    elif fault == "v3":
        row["context"]["v3_text_hash"] = "foreign"
    elif fault == "v4":
        row["context"]["v4_control_exact"] = None
    elif fault == "v4audit":
        row["context"]["roles"]["base"]["hunks"][0]["selected_changed_old"] += 1
    controls = {} if fault == "fictional" else {old["member"]["commit_id"]: (old3, old4)}
    with pytest.raises(SemanticError):
        p.validate_controls([row], [old], controls)
