"""上传导入的契约测试：列映射、体检与数据集准备阶段的可消费性。"""

import pytest

from packages.mining import asset_import
from packages.mining import public_assets as assets
from packages.mining import research_dataset as research
from packages.mining.public_dataset import NUMERIC_FIELDS

HEADER = ["sha", "repo", "label", "when", *NUMERIC_FIELDS]
MAPPING = {"commit_id": "sha", "project": "repo", "buggy": "label", "author_date": "when"}
MAPPING.update({name: name for name in NUMERIC_FIELDS})
POLICY = {
    "purpose": "published_binary_12_v1",
    "split_strategy": "time_ordered",
    "train_ratio": 0.7,
    "validation_ratio": 0.15,
    "test_ratio": 0.15,
}


def rows(count=12):
    lines = [",".join(HEADER)]
    for index in range(1, count + 1):
        lines.append(
            ",".join(
                [
                    f"{index:040x}",
                    "apache/demo",
                    "True" if index % 2 == 0 else "False",
                    str(1_041_379_200 + index * 3600),
                    *[str(index + offset) for offset in range(len(NUMERIC_FIELDS))],
                ]
            )
        )
    return "\n".join(lines) + "\n"


def store():
    return assets.ROOT / "runtime/public-assets"


def test_import_creates_a_readable_asset():
    content = rows(6)
    first = asset_import.import_upload(
        content, MAPPING, license="MIT", source_name="demo.csv", user_id="tester"
    )
    assert first["rows"] == 6
    assert first["labels"] == {"buggy": 3, "clean": 3}
    manifest = assets.load_asset(store() / first["asset_id"])["manifest"]
    assert assets.kind_of(manifest) == "upload"
    again = asset_import.import_upload(
        content, MAPPING, license="MIT", source_name="demo.csv", user_id="tester"
    )
    assert again["asset_id"] == first["asset_id"]


def test_missing_mapping_lists_the_field():
    with pytest.raises(asset_import.UploadError) as caught:
        asset_import.import_upload(
            rows(4),
            {k: v for k, v in MAPPING.items() if k != NUMERIC_FIELDS[0]},
            license="MIT",
            source_name="demo.csv",
            user_id="tester",
        )
    assert caught.value.code == "PUBLIC_MAPPING_INCOMPLETE"
    assert NUMERIC_FIELDS[0] in caught.value.detail


def test_unknown_column_is_rejected():
    mapping = dict(MAPPING, la="no-such-column")
    with pytest.raises(asset_import.UploadError) as caught:
        asset_import.import_upload(
            rows(4), mapping, license="MIT", source_name="demo.csv", user_id="tester"
        )
    assert caught.value.code == "PUBLIC_MAPPING_UNKNOWN"


def test_bad_label_is_rejected():
    content = rows(4).replace(",True,", ",maybe,", 1)
    with pytest.raises(asset_import.UploadError) as caught:
        asset_import.import_upload(
            content, MAPPING, license="MIT", source_name="demo.csv", user_id="tester"
        )
    assert caught.value.code == "PUBLIC_LABEL_INVALID"


def test_negative_feature_is_rejected():
    content = rows(4).replace(",1,", ",-1,", 1)
    with pytest.raises(asset_import.UploadError) as caught:
        asset_import.import_upload(
            content, MAPPING, license="MIT", source_name="demo.csv", user_id="tester"
        )
    assert caught.value.code == "PUBLIC_NUMERIC_INVALID"


def test_duplicate_identity_is_rejected():
    lines = rows(3).splitlines()
    content = "\n".join([*lines, lines[1]]) + "\n"
    with pytest.raises(asset_import.UploadError) as caught:
        asset_import.import_upload(
            content, MAPPING, license="MIT", source_name="demo.csv", user_id="tester"
        )
    assert caught.value.code == "PUBLIC_DUPLICATE_SAMPLE"


def test_imported_asset_feeds_dataset_preparation():
    imported = asset_import.import_upload(
        rows(20), MAPPING, license="MIT", source_name="demo.csv", user_id="tester"
    )
    directory = store() / imported["asset_id"]
    members, metadata, plan_hash = research.prepare(directory, imported["source_hash"], POLICY)
    assert len(members) == 20
    assert metadata["capabilities"]["research_binary"] is True
    assert len(plan_hash) == 64
