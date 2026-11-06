"""压测口径的纯函数测试：分位数、有效请求判定与分组汇总。"""

import pytest

from scripts.probe_latency import classify, percentile, summarize


def record(
    target="risk-items.page",
    group="serial",
    phase="measure",
    seq=1,
    elapsed=10.0,
    valid=True,
    status=200,
    reason="",
):
    clock = float(seq)
    return {
        "target": target,
        "group": group,
        "phase": phase,
        "seq": seq,
        "started_at": 0.0,
        "started_at_clock": clock,
        "finished_at": clock + elapsed / 1000,
        "elapsed_ms": elapsed,
        "status": status,
        "valid": valid,
        "reason": reason,
    }


def test_percentile_rejects_empty_sample():
    with pytest.raises(ValueError):
        percentile([], 0.95)


def test_percentile_rejects_out_of_range_fraction():
    with pytest.raises(ValueError):
        percentile([1.0], 0.0)


def test_percentile_single_sample_is_that_sample():
    assert percentile([7.5], 0.5) == 7.5
    assert percentile([7.5], 0.99) == 7.5


def test_percentile_uses_nearest_rank_with_ties():
    samples = [1.0] * 90 + [500.0] * 10
    assert percentile(samples, 0.5) == 1.0
    assert percentile(samples, 0.95) == 500.0
    assert percentile(samples, 1.0) == 500.0


def test_classify_rejects_non_200():
    assert classify(500, {"items": [], "total": 0}, ("items",)) == (False, "http_500")


def test_classify_rejects_non_object_body():
    assert classify(200, [], ("items",)) == (False, "body_not_object")


def test_classify_reports_missing_keys():
    valid, reason = classify(200, {"items": []}, ("items", "total"))
    assert valid is False and reason == "missing:total"


def test_classify_accepts_complete_body():
    assert classify(200, {"items": [], "total": 0}, ("items", "total")) == (True, "")


def test_summarize_drops_warmup_and_invalid_samples():
    records = [
        record(phase="warmup", elapsed=900.0),
        record(seq=1, elapsed=10.0),
        record(seq=2, elapsed=20.0),
        record(seq=3, elapsed=30.0, valid=False, status=503, reason="http_503"),
    ]
    entry = summarize(records, minimum=2)[0]
    assert entry["requests"] == 3
    assert entry["valid"] == 2
    assert entry["invalid"] == 1
    assert entry["p50_ms"] == 10.0
    assert entry["error_rate"] == pytest.approx(0.3333, abs=1e-4)
    assert entry["meets_minimum"] is True


def test_summarize_flags_insufficient_valid_requests():
    entry = summarize([record(seq=1, elapsed=10.0)], minimum=100)[0]
    assert entry["meets_minimum"] is False
    assert entry["p95_ms"] == 10.0


def test_summarize_keeps_groups_apart():
    records = [
        record(seq=1, elapsed=10.0),
        record(seq=2, elapsed=99.0, group="concurrency-4"),
    ]
    summary = {entry["group"]: entry for entry in summarize(records, minimum=1)}
    assert summary["serial"]["p95_ms"] == 10.0
    assert summary["concurrency-4"]["p95_ms"] == 99.0


def test_summarize_with_only_invalid_samples_has_no_percentiles():
    entry = summarize([record(seq=1, elapsed=5.0, valid=False, status=500)], minimum=1)[0]
    assert entry["valid"] == 0
    assert entry["p95_ms"] is None
    assert entry["error_rate"] == 1.0
