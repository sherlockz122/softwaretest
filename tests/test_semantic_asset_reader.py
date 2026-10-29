import copy
import json
import shutil
import struct
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

from packages.mining import public_assets as assets
from packages.mining.source_evidence import content_hash
from packages.semantic import asset_catalog as catalog
from packages.semantic import asset_reader as reader
from tests.semantic_asset_support import record, reseal, source, write


@pytest.fixture(scope="module")
def original():
    parent = assets.ROOT / "runtime/temp"
    parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=parent, prefix="asset-source-") as temp:
        yield source(Path(temp))


@pytest.fixture
def env(original):
    parent = assets.ROOT / "runtime/temp"
    with TemporaryDirectory(dir=parent, prefix="asset-reader-") as temp:
        root = Path(temp)
        directory, m = original
        target = root / directory.name
        shutil.copytree(directory, target)
        yield root, target, copy.deepcopy(m)


def test_independent_readback_duplicate_install_and_restore(env):
    root, directory, m = env
    assert directory.name == content_hash(m) and directory.name != m["content_hash"]
    installed = catalog.install(directory, m["content_hash"], store=root / "catalog")
    assert catalog.install(directory, m["content_hash"], store=root / "catalog") == installed
    value, rows, coverage = catalog.verify(installed)
    assert rows == json.loads((directory / "rows.json").read_text())["records"]
    assert coverage == json.loads((directory / "coverage.json").read_text())["records"]
    for name in reader.PAYLOADS:
        assert (installed / name).read_bytes() == (directory / name).read_bytes()
    restored = root / "restored" / installed.name
    shutil.copytree(installed, restored)
    shutil.rmtree(directory)
    assert catalog.verify(restored) == (value, rows, coverage)
    detail = catalog.detail(restored.name, 170, 100, "validation", store=restored.parent)
    assert detail["total"] == 172 and len(detail["items"]) == 2
    assert detail["items"][0]["population_index"] == 658
    assert detail["capabilities"] == catalog.CAPABILITIES
    assert not any(
        detail["capabilities"][k] for k in detail["capabilities"] if k != "structurally_readable"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "future"),
        ("accepted", True),
        ("shape", [660, 3077]),
        ("candidate_release", True),
        ("old_model_compatible", True),
        ("new_classifier_fitted", True),
        ("chunks", ["a" * 64] * 7),
        ("operations", []),
        ("binding", None),
    ],
)
def test_origin_schema_and_qualification_reject(env, field, value):
    root, directory, m = env
    m[field] = value
    directory, m = reseal(directory, m)
    with pytest.raises(reader.ConsumptionError):
        catalog.install(directory, m["content_hash"], store=root / "catalog")
    assert not (root / "catalog").exists()


@pytest.mark.parametrize(
    "case",
    [
        "identity",
        "partition",
        "index",
        "audit_member",
        "role",
        "empty",
        "omitted",
        "starts",
        "integer_bool",
        "token_cap",
    ],
)
def test_independent_member_and_coverage_corruption(env, case):
    _, directory, m = env
    rows = json.loads((directory / "rows.json").read_text())
    coverage = json.loads((directory / "coverage.json").read_text())
    r, a = rows["records"], coverage["records"]
    if case == "identity":
        r[1]["member"] = copy.deepcopy(r[0]["member"])
    elif case == "partition":
        r[0]["member"]["partition"] = "test"
    elif case == "index":
        r[0]["population_index"] = True
    elif case == "audit_member":
        a[0]["member"]["commit_id"] = "f" * 40
    elif case == "role":
        a[0]["roles"]["future"] = {}
    else:
        role = a[0]["roles"]["message"]
        key, value = {
            "empty": ("empty", True),
            "omitted": ("omitted_tokens", 1),
            "starts": ("selected_starts", [1]),
            "integer_bool": ("tokens", True),
            "token_cap": ("tokens", 253),
        }[case]
        role[key] = value
    write(directory / "rows.json", rows)
    write(directory / "coverage.json", coverage)
    directory, m = reseal(directory, m, refresh_files=True)
    with pytest.raises(reader.ConsumptionError):
        reader.members(directory, m, reader.Budget())


@pytest.mark.parametrize(
    "case",
    [
        "object",
        "float64",
        "fortran",
        "shape",
        "huge_header",
        "short",
        "extra",
        "nan",
        "infinite",
        "mask",
        "empty_nonzero",
    ],
)
def test_npy_preflight_and_values_reject_without_numpy_load(env, case):
    _, directory, m = env
    path = directory / "embeddings.npy"
    coverage = json.loads((directory / "coverage.json").read_text())["records"]
    raw = bytearray(path.read_bytes())
    if case in {"object", "float64", "fortran", "shape"}:
        left, right = {
            "object": (b"'<f4'", b"'|O4'"),
            "float64": (b"'<f4'", b"'<f8'"),
            "fortran": (b"False", b"True "),
            "shape": (b"3076", b"3077"),
        }[case]
        raw = raw.replace(left, right, 1)
    elif case == "huge_header":
        raw[8:10] = (65535).to_bytes(2, "little")
    elif case == "short":
        raw = raw[:-1]
    elif case == "extra":
        raw += b"x"
    else:
        start = 10 + int.from_bytes(raw[8:10], "little")
        if case == "empty_nonzero":
            coverage[0]["roles"]["message"]["tokens"] = 0
            raw[start + 3072 * 4 : start + 3073 * 4] = struct.pack("<f", 0)
        else:
            offset, value = {
                "nan": (0, float("nan")),
                "infinite": (0, float("inf")),
                "mask": (3072, 0.0),
            }[case]
            raw[start + offset * 4 : start + (offset + 1) * 4] = struct.pack("<f", value)
    path.write_bytes(raw)
    with pytest.raises(reader.ConsumptionError):
        reader.matrix(path, record(path), coverage, reader.Budget())


def test_oversized_metadata_and_declaration_fail_before_payload_allocation(env, monkeypatch):
    _, directory, m = env
    m["files"]["coverage.json"]["bytes"] = 1024**3
    directory, m = reseal(directory, m)
    monkeypatch.setattr(reader, "payload", lambda *a: pytest.fail("payload must not be read"))
    with pytest.raises(reader.ConsumptionError):
        reader.origin(directory, m["content_hash"], reader.Budget())
    path = directory / "large.json"
    with path.open("wb") as stream:
        stream.truncate(1048577)
    with pytest.raises(reader.ConsumptionError, match="JSON_LIMIT"):
        reader.read_json(path, 1048576, reader.Budget())


@pytest.mark.parametrize(
    "raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}', b"\xff", b"{", b"[" * 2000]
)
def test_json_corruption_stable_safe_error(env, raw):
    root, _, _ = env
    path = root / "bad.json"
    path.write_bytes(raw)
    with pytest.raises(reader.ConsumptionError) as error:
        reader.read_json(path, 65536, reader.Budget())
    assert str(root) not in str(error.value)


def test_cancel_capacity_and_deadline_never_publish(env, monkeypatch):
    root, directory, m = env

    def stop():
        raise reader.ConsumptionError("TEST_CANCELLED")

    with pytest.raises(reader.ConsumptionError, match="TEST_CANCELLED"):
        catalog.install(
            directory, m["content_hash"], store=root / "cancel", budget=reader.Budget(stop)
        )
    with pytest.raises(reader.ConsumptionError, match="DEADLINE"):
        budget = reader.Budget()
        budget.deadline = -1
        catalog.install(directory, m["content_hash"], store=root / "deadline", budget=budget)
    monkeypatch.setattr(catalog.shutil, "disk_usage", lambda p: SimpleNamespace(free=1))
    with pytest.raises(reader.ConsumptionError, match="CAPACITY"):
        catalog.install(directory, m["content_hash"], store=root / "capacity")
    assert not [
        p
        for parent in (root / "cancel", root / "deadline", root / "capacity")
        if parent.exists()
        for p in parent.iterdir()
        if reader.digest_id(p.name)
    ]


def test_mid_copy_drift_keeps_existing_catalog_and_attempt(env, monkeypatch):
    root, directory, m = env
    old_copy = catalog._copy

    def drift(source_path, *args):
        result = old_copy(source_path, *args)
        if source_path.name == "rows.json":
            source_path.write_bytes(source_path.read_bytes() + b" ")
        return result

    monkeypatch.setattr(catalog, "_copy", drift)
    with pytest.raises(reader.ConsumptionError):
        catalog.install(directory, m["content_hash"], store=root / "catalog")
    assert catalog.catalog_ids(root / "catalog") == []
    assert list((root / "catalog").glob(".install-*"))


def test_catalog_safe_ids_limits_and_missing(env):
    root, _, _ = env
    assert catalog.listing(store=root / "missing") == {
        "items": [],
        "total": 0,
        "page": 1,
        "page_size": 20,
    }
    with pytest.raises(reader.ConsumptionError) as error:
        catalog.detail("a" * 64, store=root / "missing")
    assert error.value.status == 404
    with pytest.raises(reader.ConsumptionError):
        catalog.detail("../outside", store=root)
    with pytest.raises(reader.ConsumptionError):
        reader.safe(assets.ROOT / "pyproject.toml")
    for i in range(65):
        (root / f"{i:064x}").mkdir()
    with pytest.raises(reader.ConsumptionError, match="CATALOG_LIMIT"):
        catalog.catalog_ids(root)


def test_expected_origin_and_original_seal_are_independent_gates(env):
    root, directory, manifest = env
    with pytest.raises(reader.ConsumptionError, match="ORIGIN_DRIFT"):
        catalog.install(directory, "f" * 64, store=root / "catalog")
    manifest["accepted"] = 659
    write(directory / "manifest.json", manifest)
    with pytest.raises(reader.ConsumptionError, match="SEAL"):
        catalog.install(directory, manifest["content_hash"], store=root / "catalog")


def test_json_same_length_change_during_open_read_is_rejected(env, monkeypatch):
    root, _, _ = env
    path = root / "race.json"
    path.write_bytes(b'{"x":1}')
    expected = record(path)
    original_open = Path.open

    class Changed:
        def __enter__(self):
            self.stream = original_open(path, "rb")
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            with original_open(path, "wb") as other:
                other.write(b'{"x":2}')
            return self.stream.read(size)

    monkeypatch.setattr(
        Path,
        "open",
        lambda p, *a, **kw: Changed() if p == path and a == ("rb",) else original_open(p, *a, **kw),
    )
    with pytest.raises(reader.ConsumptionError, match="DRIFT"):
        reader.read_json(path, 65536, reader.Budget(), expected)


def test_conflicting_and_concurrent_publications_preserve_existing_data(env):
    root, directory, m = env
    store = root / "installed"
    store.mkdir()
    lock = store / ".publication-lock"
    lock.write_bytes(b"existing operator")
    with pytest.raises(reader.ConsumptionError, match="INSTALL_BUSY"):
        catalog.install(directory, m["content_hash"], store=store)
    assert lock.read_bytes() == b"existing operator" and catalog.catalog_ids(store) == []
    lock.unlink()
    installed = catalog.install(directory, m["content_hash"], store=store)
    sentinel = (installed / "coverage.json").read_bytes() + b" "
    (installed / "coverage.json").write_bytes(sentinel)
    with pytest.raises(reader.ConsumptionError):
        catalog.install(directory, m["content_hash"], store=store)
    assert (installed / "coverage.json").read_bytes() == sentinel


def test_np_duplicate_header_key_is_rejected(env):
    _, directory, _ = env
    path = directory / "embeddings.npy"
    raw = path.read_bytes()
    start = 10 + int.from_bytes(raw[8:10], "little")
    header = b"{'descr':'|O4','descr':'<f4','fortran_order':False,'shape':(660,3076)}\n"
    path.write_bytes(raw[:8] + len(header).to_bytes(2, "little") + header + raw[start:])
    coverage = json.loads((directory / "coverage.json").read_text())["records"]
    with pytest.raises(reader.ConsumptionError, match="NPY_SCHEMA"):
        reader.matrix(path, record(path), coverage, reader.Budget())
