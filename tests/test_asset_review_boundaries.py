"""Regression for immutable namespaces and conservative negative ancestry evidence."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from packages.mining import asset_reviews, public_assets
from packages.mining.public_assets import AssetError
from packages.mining.source_evidence import canonical
from scripts import probe_review_links
from tests.test_public_assets import ACQUIRED, review_context, update, workspace


def test_pending_atomic_review_is_not_a_committed_revision(monkeypatch):
    with workspace() as root:
        directory, _, case, _, _ = review_context(root)
        pending, release = Event(), Event()
        write = public_assets.write_json

        def pause(path, value):
            write(path, value)
            if path.name.startswith(".000001.json.") and not pending.is_set():
                pending.set()
                assert release.wait(10)

        monkeypatch.setattr(public_assets, "write_json", pause)
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(update, directory, case)
            try:
                assert pending.wait(10)
                history = asset_reviews.case_history(directory, case["case_id"])
                assert len(history["records"]) == 1
                assert update(directory, case)["revision"] == 1
            finally:
                release.set()
            with pytest.raises(AssetError, match="REVIEW_CONFLICT"):
                first.result(timeout=10)
        history = asset_reviews.case_history(directory, case["case_id"])
        assert len(history["records"]) == 2
        assert not list((directory / "cases" / case["case_id"]).glob("*.part"))


def test_private_stage_removed_during_path_validation(monkeypatch):
    with workspace() as root:
        directory, _, case, _, _ = review_context(root)
        folder = directory / "cases" / case["case_id"]
        folder.mkdir(parents=True)
        part = folder / f".000001.json.{'a' * 32}.part"
        part.write_text("inert unpublished staging")
        stat = Path.stat
        removed = False

        def remove_after_stat(path, *args, **kwargs):
            nonlocal removed
            value = stat(path, *args, **kwargs)
            if path == part and not removed and kwargs.get("follow_symlinks", True):
                part.unlink()
                removed = True
            return value

        monkeypatch.setattr(Path, "stat", remove_after_stat)
        assert len(asset_reviews.case_history(directory, case["case_id"])["records"]) == 1
        assert removed and not part.exists()


@pytest.mark.parametrize("kind", ["malformed", "directory", "revision", "excess"])
def test_pending_review_names_remain_strict_and_bounded(kind):
    with workspace() as root:
        directory, _, case, _, _ = review_context(root)
        folder = directory / "cases" / case["case_id"]
        folder.mkdir(parents=True)
        if kind == "malformed":
            (folder / ".000001.json.invalid.part").write_text("inert")
        elif kind == "directory":
            (folder / f".000001.json.{'a' * 32}.part").mkdir()
        elif kind == "revision":
            (folder / f".000000.json.{'a' * 32}.part").write_text("inert")
        else:
            for i in range(asset_reviews.MAX_REVISIONS + 1):
                (folder / f".000001.json.{i:032x}.part").write_text("inert")
        code = "REVIEW_REVISION_LIMIT" if kind == "excess" else "REVIEW_CHAIN_INVALID"
        with pytest.raises(AssetError, match=code):
            asset_reviews.case_history(directory, case["case_id"])


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
