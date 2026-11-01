"""Independent archive attacks and real Git histories for the offline trust boundary."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from packages.mining import public_assets as assets
from packages.repositories.safety import RepositoryError
from packages.semantic import native_runtime, supplement_policy
from packages.semantic import offline_source as offline
from packages.semantic.policy import SemanticError


def sha(body):
    return hashlib.sha256(body).hexdigest()


def git(bare, *args, data=None):
    env = {k: v for k, v in os.environ.items() if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR"}}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="fixture",
        GIT_COMMITTER_NAME="fixture",
        GIT_AUTHOR_EMAIL="fixture@example.invalid",
        GIT_COMMITTER_EMAIL="fixture@example.invalid",
        GIT_AUTHOR_DATE="2020-01-01T00:00:00Z",
        GIT_COMMITTER_DATE="2020-01-01T00:00:00Z",
    )
    return subprocess.run(
        [shutil.which("git"), "-C", str(bare), *args],
        input=data,
        capture_output=True,
        env=env,
        check=True,
        timeout=20,
    ).stdout


def write_bundle(archive, files):
    files = dict(files)
    files["SHA256SUMS.txt"] = "".join(
        sha(body) + "  " + name + "\n"
        for name, body in sorted(files.items())
        if name != "SHA256SUMS.txt"
    ).encode()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as stream:
        for name, body in files.items():
            stream.writestr(offline.BUNDLE_NAME + "/" + name, body)
    return files


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(assets, "ROOT", tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    files, sources, pairs = {}, [], []
    for index, short in enumerate(("commons-collections", "commons-codec")):
        bare = runtime / (short + ".git")
        bare.mkdir()
        git(bare, "init", "--bare", "--initial-branch=pinned", "--template=")
        license_body, notice = b"Original fixture LICENSE\n", b"Original fixture NOTICE\n"
        blob = git(bare, "hash-object", "-w", "--stdin", data=license_body).strip()
        other = git(bare, "hash-object", "-w", "--stdin", data=notice).strip()
        tree = (
            git(
                bare,
                "mktree",
                data=b"100644 blob "
                + blob
                + b"\tLICENSE\n"
                + b"100644 blob "
                + other
                + b"\tNOTICE\n",
            )
            .strip()
            .decode()
        )
        root = git(bare, "commit-tree", tree, data=f"root {index}\n".encode()).strip().decode()
        left = git(bare, "commit-tree", tree, "-p", root, data=b"left\n").strip().decode()
        right = git(bare, "commit-tree", tree, "-p", root, data=b"right\n").strip().decode()
        head = (
            git(bare, "commit-tree", tree, "-p", left, "-p", right, data=b"merge\n")
            .strip()
            .decode()
        )
        git(bare, "update-ref", "refs/heads/pinned", head)
        git(bare, "repack", "-ad")
        repository = "apache/" + short
        pairs.append((repository, head))
        source = {
            "name": repository,
            "official_url": f"https://github.com/{repository}.git",
            "pinned_commit": head,
            "acquired_utc": "2026-01-01T00:00:00Z",
            "reachable_commit_count": 4,
            "reachable_object_count": 7,
            "object_type_counts": {"commit": 4, "tree": 1, "blob": 2},
            "root_commits": [root],
        }
        sources.append(source)
        for path in (bare / "objects/pack").iterdir():
            if path.suffix in {".pack", ".idx", ".rev"}:
                files[f"repositories/{short}.git/objects/pack/{path.name}"] = path.read_bytes()
        files[f"repositories/{short}.git/HEAD"] = b"ref: refs/heads/pinned\n"
        files[f"repositories/{short}.git/refs/heads/pinned"] = (head + "\n").encode()
        # Never execute the supplied configuration or a supplied tool.
        files[f"repositories/{short}.git/config"] = b"[include]\n path = ../DO-NOT-READ\n"
        files[f"licenses/{short}/LICENSE"] = license_body
        files[f"licenses/{short}/NOTICE"] = notice
    files["tools/untrusted.py"] = b"raise RuntimeError('MUST NEVER EXECUTE')\n"
    files["manifest.json"] = json.dumps(
        {"source_count": 2, "sources": sources, "network": {"unverified_external_claim": True}}
    ).encode()
    archive = runtime / "input.zip"
    files = write_bundle(archive, files)
    monkeypatch.setattr(offline, "ARCHIVE_SHA256", sha(archive.read_bytes()))
    monkeypatch.setattr(offline, "SOURCE_PAIRS", tuple(pairs))

    def approved(repository, head):
        if (repository, head) not in pairs:
            raise SemanticError("SUPPLEMENT_SOURCE_NOT_APPROVED")
        return {
            "repository": repository,
            "head": head,
            "url": f"https://github.com/{repository}.git",
        }

    monkeypatch.setattr(supplement_policy, "approved_source", approved)
    monkeypatch.setattr(offline, "bindings", lambda: {"fixture-execution": "1" * 64})
    return archive, runtime / "imports", files


def repack(bundle, monkeypatch, change):
    archive, _, files = bundle
    files = {k: v for k, v in files.items() if k != "SHA256SUMS.txt"}
    change(files)
    write_bundle(archive, files)
    monkeypatch.setattr(offline, "ARCHIVE_SHA256", sha(archive.read_bytes()))


def no_success(output):
    assert not any(p.name == offline.ARCHIVE_SHA256 for p in output.glob("*"))
    assert not list(output.glob(".offline-*/receipt.json"))


def test_real_merge_graph_import_and_independent_copied_root_consumption(bundle):
    archive, output, _ = bundle
    before = sha(archive.read_bytes())
    target = offline.admit(archive, output)
    receipt = assets.read_json(target / "receipt.json", lambda: None, maximum=4 * 1024**2)
    assert receipt["archive"]["sha256"] == before == sha(archive.read_bytes())
    assert receipt["training_ready"] is False and receipt["candidate_release"] is False
    assert receipt["online_acquisition_certified"] is False
    assert receipt["external_provenance"]["network"]["unverified_external_claim"] is True
    assert receipt["copied_root_replay_exact"] is True
    assert all(s["commits"] == 4 and s["objects"] == 7 for s in receipt["sources"])
    assert all(s["parent_edges"] == 4 for s in receipt["sources"])
    assert all(s["external_acquired_at"] < receipt["verified_at"] for s in receipt["sources"])
    assert all(c["cleanup_complete"] for c in receipt["commands"])
    restored = output.parent / "restored" / target.name
    shutil.copytree(offline.filesystem_path(target), offline.filesystem_path(restored))
    assert offline.verify(archive, restored)["content_hash"] == receipt["content_hash"]
    assert not (restored / "acquisition.json").exists()


def test_repeated_import_refuses_without_modifying_prior_publication(bundle):
    archive, output, _ = bundle
    target = offline.admit(archive, output)
    receipt = (target / "receipt.json").read_bytes()
    with pytest.raises(SemanticError, match="OFFLINE_TARGET_EXISTS"):
        offline.admit(archive, output)
    assert (target / "receipt.json").read_bytes() == receipt
    assert offline.verify(archive, target)["archive"]["sha256"] == offline.ARCHIVE_SHA256


def test_unapproved_archive_digest_refuses_before_publication(bundle):
    archive, output, _ = bundle
    with archive.open("ab") as stream:
        stream.write(b"altered")
    with pytest.raises(SemanticError, match="OFFLINE_ARCHIVE_IDENTITY"):
        offline.admit(archive, output)
    no_success(output)


@pytest.mark.parametrize(
    "name",
    [
        "../outside",
        "/absolute",
        "C:/drive",
        "a\\evil",
        "a/../evil",
        "a//evil",
        "CON.txt",
        "dir/NUL",
        "COM1.log",
        "x.",
        "x ",
        "stream:ads",
        "./x",
    ],
)
def test_unsafe_member_paths_are_rejected_before_extraction(bundle, monkeypatch, name):
    archive, output, _ = bundle
    with zipfile.ZipFile(archive, "a") as stream:
        info = zipfile.ZipInfo("placeholder")
        # Windows ZipInfo construction normalizes backslashes; preserve the hostile raw name.
        info.filename = info.orig_filename = offline.BUNDLE_NAME + "/" + name
        stream.writestr(info, b"bad")
    monkeypatch.setattr(offline, "ARCHIVE_SHA256", sha(archive.read_bytes()))
    with pytest.raises(SemanticError, match="OFFLINE_ARCHIVE_PATH"):
        offline.admit(archive, output)
    no_success(output)


@pytest.mark.parametrize("attack", ["case", "directory_collision", "symlink", "compression"])
def test_archive_structure_attacks(bundle, monkeypatch, attack):
    archive, output, _ = bundle
    with zipfile.ZipFile(archive, "a") as stream:
        name = {
            "case": "MANIFEST.json",
            "directory_collision": "manifest.json/child",
            "symlink": "link",
            "compression": "compressed",
        }[attack]
        info = zipfile.ZipInfo(offline.BUNDLE_NAME + "/" + name)
        if attack == "symlink":
            info.create_system = 3
            info.external_attr = 0o120777 << 16
        if attack == "compression":
            info.compress_type = zipfile.ZIP_LZMA
        stream.writestr(info, b"payload")
    monkeypatch.setattr(offline, "ARCHIVE_SHA256", sha(archive.read_bytes()))
    with pytest.raises(SemanticError, match="OFFLINE_ARCHIVE_"):
        offline.admit(archive, output)
    no_success(output)


@pytest.mark.parametrize("attack", ["missing_hash", "wrong_hash", "duplicate_hash"])
def test_complete_checksum_coverage_is_required(bundle, monkeypatch, attack):
    archive, output, files = bundle
    lines = files["SHA256SUMS.txt"].splitlines(keepends=True)
    files = dict(files)
    files["SHA256SUMS.txt"] = b"".join(
        lines[1:]
        if attack == "missing_hash"
        else [b"0" * 64 + lines[0][64:], *lines[1:]]
        if attack == "wrong_hash"
        else lines + lines[:1]
    )
    with zipfile.ZipFile(archive, "w") as stream:
        for n, body in files.items():
            stream.writestr(offline.BUNDLE_NAME + "/" + n, body)
    monkeypatch.setattr(offline, "ARCHIVE_SHA256", sha(archive.read_bytes()))
    with pytest.raises(SemanticError, match="OFFLINE_CHECKSUM"):
        offline.admit(archive, output)
    no_success(output)


@pytest.mark.parametrize("attack", ["unknown_head", "duplicate_source", "license", "pack", "hook"])
def test_source_claims_and_object_integrity_are_not_self_authorizing(bundle, monkeypatch, attack):
    archive, output, _ = bundle

    def change(files):
        if attack in {"unknown_head", "duplicate_source"}:
            manifest = json.loads(files["manifest.json"])
            if attack == "unknown_head":
                manifest["sources"][0]["pinned_commit"] = "0" * 40
            else:
                manifest["sources"][1] = manifest["sources"][0]
            files["manifest.json"] = json.dumps(manifest).encode()
        elif attack == "license":
            files["licenses/commons-codec/LICENSE"] = b"forged license"
        elif attack == "hook":
            files["repositories/commons-codec.git/hooks/pre-commit"] = b"exit 0"
        else:
            name = next(n for n in files if n.endswith(".pack"))
            files[name] = files[name][:20] + b"broken"

    repack(bundle, monkeypatch, change)
    with pytest.raises((SemanticError, RepositoryError)):
        offline.admit(archive, output)
    no_success(output)
    assert list(output.glob(".offline-*/failure.json"))


@pytest.mark.parametrize(
    "limit",
    [
        "MAX_ARCHIVE_BYTES",
        "MAX_MEMBER_BYTES",
        "MAX_MEMBERS",
        "MAX_EXTRACTED_BYTES",
        "MAX_GIT_OUTPUT",
        "MAX_RSS",
    ],
)
def test_hard_resource_limits_cannot_publish_success(bundle, monkeypatch, limit):
    archive, output, _ = bundle
    monkeypatch.setattr(offline, limit, 1)
    with pytest.raises((SemanticError, RepositoryError)):
        offline.admit(archive, output)
    no_success(output)


def test_deadline_and_low_storage_are_independent_refusals(bundle, monkeypatch):
    archive, output, _ = bundle
    with monkeypatch.context() as patch:
        patch.setattr(offline, "MAX_SECONDS", 0)
        with pytest.raises(SemanticError, match="OFFLINE_TIMEOUT"):
            offline.admit(archive, output)
    with monkeypatch.context() as patch:
        patch.setattr(shutil, "disk_usage", lambda _: shutil._ntuple_diskusage(10, 9, 1))
        with pytest.raises(SemanticError, match="OFFLINE_LOW_STORAGE"):
            offline.admit(archive, output)
    no_success(output)


def test_runtime_only_output_and_original_input_do_not_overlap(bundle):
    archive, output, _ = bundle
    with pytest.raises(assets.AssetError):
        offline.admit(archive, output.parent.parent / "outside-runtime")
    assert not (output.parent.parent / "outside-runtime").exists()


def test_credential_free_offline_commands_and_cancellation_cleanup(bundle, monkeypatch):
    archive, output, _ = bundle
    real = native_runtime.run
    observed = []
    monkeypatch.setenv("GIT_SSH_COMMAND", "untrusted")
    monkeypatch.setenv("GITHUB_TOKEN", "inert-fixture")

    def run(arguments, **kwargs):
        assert "GITHUB_TOKEN" not in kwargs["environment"]
        assert "GIT_SSH_COMMAND" not in kwargs["environment"]
        assert kwargs["environment"]["GIT_ALLOW_PROTOCOL"] == ""
        assert "fetch" not in arguments and "clone" not in arguments
        observer = kwargs["observe"]

        def observe(pid, created, descendants):
            observed.extend(({pid: created} | descendants).items())
            observer(pid, created, descendants)
            if "fsck" in arguments:
                raise SemanticError("OFFLINE_TEST_CANCEL")

        kwargs["observe"] = observe
        return real(arguments, **kwargs)

    monkeypatch.setattr(native_runtime, "run", run)
    with pytest.raises(SemanticError, match="OFFLINE_TEST_CANCEL"):
        offline.admit(archive, output)
    assert observed and not any(native_runtime._live(pid, created) for pid, created in observed)
    no_success(output)


def test_cleanup_failure_cannot_be_published(bundle, monkeypatch):
    archive, output, _ = bundle
    real = native_runtime.run

    def run(*args, **kwargs):
        return replace(real(*args, **kwargs), cleanup_complete=False)

    monkeypatch.setattr(native_runtime, "run", run)
    with pytest.raises(SemanticError, match="OFFLINE_PROCESS_CLEANUP"):
        offline.admit(archive, output)
    no_success(output)


def test_code_binding_drift_between_entry_and_publication_refuses(bundle, monkeypatch):
    archive, output, _ = bundle
    count = [0]

    def binding():
        count[0] += 1
        return {"fixture": "1" * 64 if count[0] == 1 else "2" * 64}

    monkeypatch.setattr(offline, "bindings", binding)
    with pytest.raises(SemanticError, match="OFFLINE_EXECUTION_DRIFT"):
        offline.admit(archive, output)
    no_success(output)


@pytest.mark.parametrize("attack", ["payload", "resealed_source", "extra_file", "code"])
def test_published_tamper_or_reseal_never_substitutes_for_native_verification(
    bundle, monkeypatch, attack
):
    archive, output, _ = bundle
    target = offline.admit(archive, output)
    if attack == "payload":
        offline.filesystem_path(next((target / "sources").rglob("*.pack"))).write_bytes(b"corrupt")
    elif attack == "extra_file":
        (target / "unexpected").write_bytes(b"extra")
    elif attack == "code":
        monkeypatch.setattr(offline, "bindings", lambda: {"fixture-execution": "2" * 64})
    else:
        receipt = assets.read_json(target / "receipt.json", lambda: None, maximum=4 * 1024**2)
        receipt["sources"][0]["head"] = "0" * 40
        (target / "receipt.json").write_bytes(
            json.dumps(
                assets.sealed({k: v for k, v in receipt.items() if k != "content_hash"})
            ).encode()
        )
    with pytest.raises((SemanticError, RepositoryError)):
        offline.verify(archive, target)


def test_hardlinked_original_is_rejected_without_altering_either_name(bundle):
    archive, output, _ = bundle
    alias = archive.with_name("hardlinked-input.zip")
    os.link(archive, alias)
    before = archive.read_bytes()
    with pytest.raises(SemanticError, match="OFFLINE_FILE_INVALID"):
        offline.admit(archive, output)
    assert archive.read_bytes() == alias.read_bytes() == before
    no_success(output)


def test_owned_publication_lock_refuses_concurrent_import_without_deleting_lock(bundle):
    archive, output, _ = bundle
    output.mkdir()
    lock = output / (".publish-" + offline.ARCHIVE_SHA256)
    lock.mkdir()
    (lock / "owned-by-other-attempt").write_bytes(b"preserve")
    with pytest.raises(SemanticError, match="OFFLINE_PUBLICATION_BUSY"):
        offline.admit(archive, output)
    assert (lock / "owned-by-other-attempt").read_bytes() == b"preserve"
    assert not list(output.glob(".offline-*"))


def test_failure_immediately_before_publication_retains_no_success_receipt(bundle, monkeypatch):
    archive, output, _ = bundle
    original = offline.Budget.final

    def late_cancel(self):
        original(self)
        raise SemanticError("OFFLINE_TEST_LATE_CANCEL")

    monkeypatch.setattr(offline.Budget, "final", late_cancel)
    with pytest.raises(SemanticError, match="OFFLINE_TEST_LATE_CANCEL"):
        offline.admit(archive, output)
    no_success(output)
    assert list(output.glob(".offline-*/failure.json"))
    assert not list(output.glob(".publish-*"))


def test_target_created_at_commit_boundary_is_preserved_without_overwrite(bundle, monkeypatch):
    archive, output, _ = bundle
    original = assets.write_json

    def race(path, value):
        original(path, value)
        if path.name == "receipt.json":
            target = output / offline.ARCHIVE_SHA256
            target.mkdir()
            (target / "other-writer").write_bytes(b"preserve")

    monkeypatch.setattr(assets, "write_json", race)
    with pytest.raises(SemanticError, match="OFFLINE_TARGET_EXISTS"):
        offline.admit(archive, output)
    target = output / offline.ARCHIVE_SHA256
    assert (target / "other-writer").read_bytes() == b"preserve"
    assert not (target / "receipt.json").exists()
    assert not list(output.glob(".offline-*/receipt.json"))


def test_original_archive_drift_after_native_verification_refuses(bundle, monkeypatch):
    archive, output, _ = bundle
    original = offline.Budget.final

    def drift(self):
        original(self)
        with archive.open("ab") as stream:
            stream.write(b"late drift")

    monkeypatch.setattr(offline.Budget, "final", drift)
    with pytest.raises(SemanticError, match="OFFLINE_INPUT_DRIFT"):
        offline.admit(archive, output)
    no_success(output)


@pytest.mark.parametrize("attack", ["config", "pack", "object_count", "readiness"])
def test_forged_receipt_with_updated_payload_hashes_still_requires_native_facts(bundle, attack):
    archive, output, _ = bundle
    target = offline.admit(archive, output)
    receipt = assets.read_json(target / "receipt.json", lambda: None, maximum=4 * 1024**2)
    if attack in {"config", "pack"}:
        payload = next((target / "sources").rglob("config" if attack == "config" else "*.pack"))
        offline.filesystem_path(payload).write_bytes(b"forged")
        receipt["files"][payload.relative_to(target).as_posix()] = {
            "bytes": 6,
            "sha256": sha(b"forged"),
        }
    elif attack == "object_count":
        receipt["sources"][0]["objects"] = 1
    else:
        receipt["training_ready"] = True
    (target / "receipt.json").write_bytes(
        json.dumps(
            assets.sealed({k: v for k, v in receipt.items() if k != "content_hash"})
        ).encode()
    )
    with pytest.raises((SemanticError, RepositoryError)):
        offline.verify(archive, target)


def test_cli_unknown_operation_is_rejected_before_work():
    result = subprocess.run(
        [sys.executable, "-m", "scripts.semantic_source_supplement", "train"],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode != 0


def test_final_digest_target_is_checked_for_a_dangling_link(bundle, monkeypatch):
    archive, output, _ = bundle
    target = output / offline.ARCHIVE_SHA256
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda p: p == target or original(p))
    with pytest.raises(assets.AssetError, match="ASSET_PATH_LINK"):
        offline.admit(archive, output)


@pytest.mark.parametrize("name", ["COM¹.txt", "LPT²", "CONIN$", "CONOUT$"])
def test_windows_device_aliases_never_become_extracted_files(name):
    with pytest.raises(SemanticError, match="OFFLINE_ARCHIVE_PATH"):
        offline.member_name(offline.BUNDLE_NAME + "/" + name)


def test_native_directory_publication_never_overwrites_an_existing_empty_target(bundle):
    _, output, _ = bundle
    source, target = output.parent / "pending", output.parent / "existing-empty"
    source.mkdir()
    target.mkdir()
    (source / "payload").write_bytes(b"pending")
    with pytest.raises(FileExistsError):
        offline.publish_directory(source, target)
    assert (source / "payload").read_bytes() == b"pending"
    assert list(target.iterdir()) == []


def test_failure_record_preserves_safe_semantic_code_without_raw_error_text(bundle):
    _, output, _ = bundle
    attempt = output.parent / "diagnostic"
    attempt.mkdir()
    offline.failure(attempt, SemanticError("OFFLINE_TEST_CANCEL"), None)
    assert json.loads((attempt / "failure.json").read_text())["code"] == "OFFLINE_TEST_CANCEL"


def test_cli_uses_the_same_safe_code_without_traceback_or_local_paths(bundle):
    archive, output, _ = bundle
    code = (
        "import sys,runpy;from pathlib import Path;"
        "from packages.mining import public_assets as a;a.ROOT=Path(sys.argv.pop(1));"
        "from packages.semantic import offline_source as s;s.bindings=lambda:{'fixture':'fixed'};"
        "runpy.run_module('scripts.semantic_source_supplement',run_name='__main__')"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(assets.ROOT),
            "offline-admit",
            "--archive",
            str(archive),
            "--output",
            str(output),
        ],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["code"] == "OFFLINE_ARCHIVE_IDENTITY"
    assert result.stderr == b"" and str(archive).encode() not in result.stdout
