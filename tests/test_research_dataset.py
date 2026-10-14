import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from packages.mining import public_assets as assets
from packages.mining import research_dataset as research
from tests.dataset_support import fixture_asset, reseal_asset

POLICY = {
    "purpose": research.PURPOSE,
    "split_strategy": "time_ordered",
    "train_ratio": 0.70,
    "validation_ratio": 0.15,
    "test_ratio": 0.15,
}


@pytest.fixture
def work():
    root = assets.ROOT / "runtime/temp"
    root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=root, prefix="dataset-unit-") as directory:
        yield Path(directory)


def test_temporal_reproducible_posthoc_and_features(work):
    directory, source = fixture_asset(work)
    first = research.prepare(directory, source["manifest"]["content_hash"], POLICY)
    second = research.prepare(directory, source["manifest"]["content_hash"], POLICY)
    assert first == second
    members, metadata, _ = first
    assert [sum(r["partition"] == p for r in members) for p in research.PARTITIONS] == [28, 6, 6]
    assert metadata["partitions"] == {
        "train": {"clean": 14, "buggy": 14},
        "validation": {"clean": 3, "buggy": 3},
        "test": {"clean": 3, "buggy": 3},
    }
    assert not set(metadata["features"]) & {"fix", "FIX", "LT"}
    assert metadata["capabilities"]["research_binary"]
    assert not metadata["capabilities"]["historical_replay"]
    assert assets.load_asset(directory)["quality"]["capabilities"] == assets.CAPABILITIES


def test_same_time_group_never_crosses_partition(work):
    def group(row, i, _):
        row["author_date"] = str(((i - 1) // 4 + 1) * 1000)

    directory, source = fixture_asset(work, mutate=group)
    members, _, _ = research.prepare(directory, source["manifest"]["content_hash"], POLICY)
    mapping = {}
    for row in members:
        mapping.setdefault(row["event_epoch"], set()).add(row["partition"])
    assert all(len(partitions) == 1 for partitions in mapping.values())


def test_canonical_duplicate_rows_merge_and_repository_holdout(work):
    projects = (
        "apache/activemq",
        "apache/camel",
        "apache/flink",
        "apache/groovy",
        "apache/hadoop-hdfs",
        "apache/hadoop-mapreduce",
        "apache/hive",
        "apache/kafka",
    )
    directory, source = fixture_asset(work, projects=projects)
    members, metadata, _ = research.prepare(
        directory,
        source["manifest"]["content_hash"],
        POLICY | {"split_strategy": "repository_holdout"},
    )
    assert (
        metadata["original_rows"] == 320
        and metadata["rows"] == 280
        and metadata["duplicate_rows"] == 40
    )
    assignments = {}
    for row in members:
        assignments.setdefault(row["canonical_repository"], set()).add(row["partition"])
    assert all(len(partitions) == 1 for partitions in assignments.values())
    assert set(
        next(r for r in members if r["canonical_repository"] == "apache/hadoop")["cohorts"]
    ) == {"apache/hadoop-hdfs", "apache/hadoop-mapreduce"}


def test_conflicting_canonical_duplicate_rejected(work):
    def conflict(row, _, project):
        if project.endswith("mapreduce"):
            row["la"] = "999"

    directory, source = fixture_asset(
        work, projects=("apache/hadoop-hdfs", "apache/hadoop-mapreduce"), mutate=conflict
    )
    with pytest.raises(research.ResearchError, match="DATA_DUPLICATE_CONFLICT"):
        research.prepare(directory, source["manifest"]["content_hash"], POLICY)


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda r, i, p: r.update(author_date="1000"), "DATA_SPLIT_UNUSABLE"),
        (lambda r, i, p: r.update(buggy="False"), "DATA_SPLIT_SINGLE_CLASS"),
    ],
)
def test_unusable_or_single_class_split_rejected(work, change, code):
    directory, source = fixture_asset(work, mutate=change)
    with pytest.raises(research.ResearchError, match=code):
        research.prepare(directory, source["manifest"]["content_hash"], POLICY)


def test_self_consistent_normalization_tamper_still_rejected(work):
    directory, _ = fixture_asset(work)
    path = directory / "normalized.jsonl"
    lines = path.read_text().splitlines()
    record = json.loads(lines[0])
    record["numeric12"]["la"] = 999
    lines[0] = json.dumps(record)
    path.write_text("\n".join(lines) + "\n")
    directory, manifest = reseal_asset(directory)
    with pytest.raises(research.ResearchError, match="DATA_NORMALIZATION_DRIFT"):
        research.prepare(directory, manifest["content_hash"], POLICY)


@pytest.mark.parametrize(
    "policy",
    [
        POLICY | {"purpose": "historical"},
        POLICY | {"train_ratio": True},
        POLICY | {"validation_ratio": 0.3},
        POLICY | {"extra": 1},
    ],
)
def test_purpose_and_policy_refused(policy):
    with pytest.raises(research.ResearchError):
        research.policy_valid(policy)


@pytest.mark.parametrize("identifier", ["../outside", "C:/arbitrary", "0" * 63, "A" * 64])
def test_no_arbitrary_source_path(identifier):
    with pytest.raises(research.ResearchError):
        research.asset_path(identifier)


def test_changed_source_and_callback_stop(work):
    directory, _ = fixture_asset(work)
    with pytest.raises(research.ResearchError, match="DATA_SOURCE_DRIFT"):
        research.prepare(directory, "0" * 64, POLICY)

    def stop():
        raise RuntimeError("test cancellation")

    with pytest.raises(RuntimeError, match="test cancellation"):
        research.prepare(directory, "0" * 64, POLICY, tick=stop)
