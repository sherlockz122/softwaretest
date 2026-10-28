"""Independent regression for mid-read source/input drift, with no real files mutated."""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from packages.mining import public_assets as assets
from packages.semantic import artifacts, coverage
from packages.semantic import diagnostics as d
from packages.semantic.policy import SemanticError
from tests.test_semantic_diagnostics import selected_rows


@pytest.mark.parametrize("failure", ["source", "input"])
def test_declaration_mid_read_drift_is_rejected(monkeypatch, failure):
    changed = [False]
    original_sources = {name: "0" * 64 for name in d.SOURCES}
    parent = {
        "version": "hunk-context-v3-bounded-probe",
        "policy": coverage.probe_policy(),
        "sources": {name: original_sources[name] for name in coverage.SOURCES},
        "content_hash": "0" * 64,
    }

    def rows(_):
        changed[0] = True
        return iter(selected_rows())

    def sources(names):
        return {
            name: (
                "1" * 64
                if failure == "source" and changed[0] and name == "packages/semantic/diagnostics.py"
                else original_sources[name]
            )
            for name in names
        }

    def verify(*_):
        return parent | ({"content_hash": "1" * 64} if failure == "input" and changed[0] else {})

    monkeypatch.setattr(artifacts, "records", rows)
    monkeypatch.setattr(artifacts, "sources", sources)
    monkeypatch.setattr(artifacts, "verify", verify)
    with TemporaryDirectory(dir=assets.ROOT / "runtime/temp", prefix="declare-guard-") as name:
        work = Path(name)
        with pytest.raises(SemanticError, match="DIAGNOSIS_DECLARATION_DRIFT"):
            d.declare(work / "input", work / "output")
        # Failed attempt may remain, but no accepted content-addressed declaration.
        assert all(p.name.startswith(".declaration-") for p in (work / "output").iterdir())
