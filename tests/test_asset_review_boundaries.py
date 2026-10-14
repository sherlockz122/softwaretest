"""Regression for immutable namespaces and conservative negative ancestry evidence."""

import pytest

from packages.mining import asset_reviews
from packages.mining.public_assets import AssetError
from packages.mining.source_evidence import canonical
from scripts import probe_review_links
from tests.test_public_assets import ACQUIRED, review_context, workspace


def test_review_initialization_cannot_enter_asset_namespace():
    with workspace() as root:
        _, _, _, report, asset = review_context(root)
        before = (asset / "manifest.json").read_bytes()
        with pytest.raises(AssetError, match="REVIEW_OUTPUT_COLLISION"):
            asset_reviews.initialize_reviews(report, asset, asset.parent)
        assert (asset / "manifest.json").read_bytes() == before


def test_native_evidence_cannot_enter_asset_namespace():
    with workspace() as root:
        directory, _, case, _, asset = review_context(root)
        with pytest.raises(AssetError, match="REVIEW_OUTPUT_COLLISION"):
            probe_review_links.probe(
                directory, case["case_id"], root / "bare", root / "receipt", asset / "evidence.json"
            )
        assert not (asset / "evidence.json").exists()


def test_nonshallow_negative_or_missing_graph_remains_unknown(monkeypatch):
    with workspace() as root:
        directory, _, case, _, _ = review_context(root)
        (root / "bare").mkdir()
        receipt = root / "receipt.json"
        receipt.write_bytes(canonical({"repository": "apache/activemq", "acquired_at": ACQUIRED}))
        for exit_code in (1, 128):

            class IncompleteReader:
                def __init__(self, *args):
                    pass

                def command(self, args, **kwargs):
                    if args[0] == "rev-parse":
                        return b"false\n"
                    if args[0] == "cat-file":
                        return 0
                    if args[0] == "show":
                        return f"{args[3]} 100 100\n".encode()
                    assert args[0] == "merge-base"
                    assert 128 in kwargs["codes"]
                    return exit_code

            monkeypatch.setattr(probe_review_links, "NativeReader", IncompleteReader)
            result = probe_review_links.probe(
                directory,
                case["case_id"],
                root / "bare",
                receipt,
                root / f"native-{exit_code}.json",
            )
            assert not result["shallow"]
            assert result["results"][0]["ancestry"] == "unknown_unverified_graph"
            assert result["label_certified"] is False
