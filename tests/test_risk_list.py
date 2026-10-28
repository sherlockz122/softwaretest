"""历史风险列表的解析、排序与分页。纯函数，不连库、不碰磁盘。"""

import csv
import io

import pytest

from packages.modeling.policy import ModelError
from packages.modeling.service import (
    PROBABILITY_HEADER,
    RISK_EVAL_PARTITIONS,
    risk_level,
    risk_page,
    risk_rows,
)


def reader(rows, header=PROBABILITY_HEADER):
    body = [",".join(header)] + [",".join(str(value) for value in row) for row in rows]
    return csv.DictReader(io.StringIO("\n".join(body) + "\n"))


def row(partition, commit, epoch, label, probability):
    return (partition, "sherlockz122/demo", commit, epoch, label, probability)


def test_默认只列验证与测试分区():
    assert RISK_EVAL_PARTITIONS == ("validation", "test")


def test_解析丢弃标签并只保留选中的分区():
    rows = risk_rows(
        reader(
            [
                row("train", "c" * 40, 1, 1, 0.9),
                row("validation", "d" * 40, 2, 0, 0.4),
                row("test", "e" * 40, 3, 1, 0.6),
            ]
        ),
        ("validation", "test"),
    )
    assert [r["commit_id"] for r in rows] == ["d" * 40, "e" * 40]
    assert set(rows[0]) == {"commit_id", "event_epoch", "partition", "probability"}
    assert "label" not in rows[0]


def test_概率降序且同概率按提交编号升序():
    rows = [
        {"commit_id": "c", "event_epoch": 1, "partition": "test", "probability": 0.5},
        {"commit_id": "a", "event_epoch": 2, "partition": "test", "probability": 0.5},
        {"commit_id": "b", "event_epoch": 3, "partition": "test", "probability": 0.9},
    ]
    result = risk_page(rows, 1, 10, 0.5, ("test",))
    assert [item["commit_id"] for item in result["items"]] == ["b", "a", "c"]


def test_翻页不重不漏():
    rows = [
        {"commit_id": f"{index:040d}", "event_epoch": index, "partition": "test",
         "probability": index / 100}
        for index in range(25)
    ]
    seen, page = [], 1
    while True:
        chunk = risk_page(rows, page, 10, 0.5, ("test",))
        seen.extend(item["commit_id"] for item in chunk["items"])
        if page * 10 >= chunk["total"]:
            break
        page += 1
    assert len(seen) == len(set(seen)) == 25
    assert len(seen) == risk_page(rows, 1, 10, 0.5, ("test",))["total"]


def test_页大小上限与非法页码():
    rows = [{"commit_id": "a", "event_epoch": 1, "partition": "test", "probability": 0.5}]
    with pytest.raises(ModelError, match="MODEL_INVALID_INPUT"):
        risk_page(rows, 1, 101, 0.5, ("test",))
    with pytest.raises(ModelError, match="MODEL_INVALID_INPUT"):
        risk_page(rows, 0, 10, 0.5, ("test",))


def test_超过行数上限明确报错而不是截断():
    stream = reader([row("test", f"{i:040d}", i, 0, 0.1) for i in range(3)])
    with pytest.raises(ModelError, match="MODEL_RISK_LIST_LIMIT"):
        risk_rows(stream, ("test",), limit=2)


def test_表头不一致视为工件漂移():
    stream = reader([("test", "a" * 40, 1, 0.5)], header=("partition", "commit_id"))
    with pytest.raises(ModelError, match="MODEL_ARTIFACT_DRIFT"):
        risk_rows(stream, ("test",))


@pytest.mark.parametrize("probability,expected", [(0.25, "high"), (0.2, "high"),
                                                  (0.19, "medium"), (0.1, "medium"),
                                                  (0.09, "low")])
def test_风险等级按运行阈值分档(probability, expected):
    assert risk_level(probability, 0.2) == expected


def test_阈值缺失时不臆造等级():
    assert risk_level(0.9, None) is None
    assert risk_level(0.9, True) is None
