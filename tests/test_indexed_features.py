"""Independent legacy oracle: exact values, reasons, source and causal boundaries."""

import random
from dataclasses import replace

import pytest

from packages.mining.causal_features import MessagePurpose, compute_causal
from packages.mining.feature_inputs import freeze_native_record
from packages.mining.features import HISTORY, FixSignal, Snapshot, compute
from packages.mining.indexed_features import compute_indexed
from tests.szz_support import reader_for, szz_history
from tests.test_features import ALICE, BOB, START, change, commit


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("coverage", ["full", "recent_window"])
def test_randomized_dag_exact_oracle(seed, coverage):
    rng = random.Random(seed)
    commits = []
    for i in range(1, 61):
        parents = [i - 1] if i > 1 else []
        if i > 3 and rng.randrange(6) == 0:
            parents.append(rng.randrange(1, i - 1))
        files = []
        for file_number in range(rng.randrange(1, 5)):
            path = f"src{rng.randrange(3)}/file{file_number}.py"
            kind = rng.choice("AMRDCT")
            files.append(
                change(
                    None if kind == "A" else path,
                    None if kind == "D" else path + ".new" if kind in "RC" else path,
                    kind,
                    la=rng.randrange(4),
                    ld=rng.randrange(4),
                    lt=rng.randrange(8),
                    is_binary=rng.randrange(25) == 0,
                )
            )
        commits.append(
            commit(
                i,
                parents,
                day=i if seed % 2 else rng.randrange(60),
                files=files,
                author=rng.choice([ALICE, BOB]),
                parse_status="message_encoding" if rng.randrange(30) == 0 else "parsed",
                fix=FixSignal(bool(i % 2), START, "oracle", 0, "e" * 64),
            )
        )
    rng.shuffle(commits)
    snapshot = Snapshot("oracle", f"{60:040x}", tuple(commits), coverage)
    assert compute_indexed(snapshot) == compute(snapshot)


@pytest.mark.parametrize("scenario", ["rename", "modify", "delete", "add", "binary", "encoding"])
def test_native_git_exact_oracle(tmp_path, scenario):
    root, _, head, shas = szz_history(tmp_path / "source", scenario=scenario, count=3)
    reader = reader_for(root, tmp_path / "attempt")
    inputs = tuple(freeze_native_record(reader, reader.record(sha)) for sha in shas)
    snapshot = Snapshot("native", shas[-1], inputs)
    assert head in shas
    assert compute_indexed(snapshot) == compute(snapshot)


def test_lineage_order_branch_exclusion_and_lifetime():
    inputs = (
        commit(1, files=[change(None, "a.py", "A", ld=0)]),
        commit(2, [1], 1, [change("a.py", "b.py", "R")]),
        commit(3, [2], 2, [change("b.py", None, "D")], BOB),
        commit(4, [3], 3, [change(None, "b.py", "A", ld=0)]),
        commit(5, [4], 4, [change("b.py", "c.py", "C")]),
        commit(6, [5], 5, [change("c.py", "c.py"), change("b.py", "b.py")], BOB),
        commit(7, [1], 3, [change("a.py", "a.py")], BOB),
        commit(8, [6, 7], 6, files=[]),
    )
    snapshot = Snapshot("lifetime", inputs[-1].sha, inputs)
    assert compute_indexed(snapshot) == compute(snapshot)
    target = next(v for v in compute_indexed(snapshot) if v.sha == inputs[5].sha)
    assert dict(target.values)["AGE"] == 1.5
    assert dict(target.values)["NDEV"] == 1


def test_same_commit_renames_do_not_chain_within_one_event():
    inputs = (
        commit(1, files=[change(None, "a.py", "A", ld=0)]),
        commit(2, [1], 1, [change("a.py", "b.py", "R"), change("b.py", "c.py", "R")]),
        commit(3, [2], 2, [change("c.py", "c.py")]),
    )
    snapshot = Snapshot("file-order", inputs[-1].sha, inputs)
    assert compute_indexed(snapshot) == compute(snapshot)


def test_inversion_zero_churn_and_unsupported_ancestors():
    for middle in (
        commit(2, [1], 9),
        commit(2, [1], 1, parse_status="message_encoding"),
        commit(2, [1], 1, [change(la=0, ld=0)]),
    ):
        inputs = (commit(1), middle, commit(3, [2], 3, [change(la=0, ld=0)]))
        snapshot = Snapshot("missing", inputs[-1].sha, inputs)
        assert compute_indexed(snapshot) == compute(snapshot)


@pytest.mark.parametrize("boundary", ["rename", "copy", "delete_readd", "fork", "inversion"])
def test_sparse_index_long_lineage_boundaries(boundary):
    inputs = [commit(1, files=[change(None, "p0.py", "A", ld=0)])]
    for i in range(2, 42):
        files = [change(f"p{i - 2}.py", f"p{i - 1}.py", "R")]
        if boundary == "copy" and i == 20:
            files = [change(f"p{i - 2}.py", f"p{i - 1}.py", "C")]
        if boundary == "delete_readd" and i == 20:
            files = [change(f"p{i - 2}.py", None, "D")]
        if boundary == "delete_readd" and i == 21:
            files = [change(None, f"p{i - 1}.py", "A", ld=0)]
        parents = [i - 1]
        if boundary == "fork" and i == 21:
            parents = [10]
        inputs.append(
            commit(
                i,
                parents,
                99 if boundary == "inversion" and i == 20 else i,
                files=files,
                author=ALICE if i % 2 else BOB,
            )
        )
    if boundary == "fork":
        inputs.append(commit(42, [20, 41], 100, files=[]))
    snapshot = Snapshot("sparse-lineage", inputs[-1].sha, tuple(inputs))
    # Maximum path frequency 2, below the dense strategy threshold; this tests
    # the indexed traversal rather than only the legacy dense fallback.
    assert compute_indexed(snapshot) == compute(snapshot)


def test_cancellation_and_capacity_exception_propagate():
    snapshot = Snapshot("cancel", commit(1).sha, (commit(1),))
    for error in (InterruptedError("cancel"), OSError("capacity")):

        def stop():
            raise error

        with pytest.raises(type(error), match=str(error)):
            compute_indexed(snapshot, tick=stop)


def test_v2_numeric_equivalence_and_future_review_isolation():
    inputs = tuple(commit(i, [i - 1] if i > 1 else [], i) for i in range(1, 30))
    snapshot = Snapshot("v2", inputs[-1].sha, inputs)
    signals = tuple(
        MessagePurpose(c.sha, i % 2, "a" * 64, "message_purpose") for i, c in enumerate(inputs)
    )
    outputs = compute_causal(snapshot, signals).as_dict()["vectors"]
    legacy = compute(snapshot)
    for row, oracle in zip(outputs, legacy, strict=True):
        for field in HISTORY:
            assert row["values"][field] == dict(oracle.values)[field]
        assert row["values"]["FIX"] in (0, 1)
    reviewed = replace(
        snapshot,
        commits=tuple(
            replace(c, fix=FixSignal(True, START, "future", 9, "b" * 64)) for c in inputs
        ),
    )
    assert compute_causal(reviewed, signals) == compute_causal(snapshot, signals)
