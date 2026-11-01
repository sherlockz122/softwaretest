"""Safety refusals must run before any source network or publication."""

from pathlib import Path

import pytest

from packages.mining import public_assets as assets
from packages.semantic import supplement_source as source
from packages.semantic.policy import SemanticError

HEAD = "5fc937c676f9c3dfe018db7cae9a2d57d33cae4e"


@pytest.mark.parametrize(
    "relative",
    [
        "shallow",
        "objects/info/alternates",
        "objects/info/http-alternates",
        "info/grafts",
        "objects/pack/input.promisor",
        "refs/replace/abc",
    ],
)
def test_unsafe_git_layout_refused_before_any_git_command(tmp_path, monkeypatch, relative):
    monkeypatch.setattr(assets, "ROOT", tmp_path)
    bare = tmp_path / "runtime" / "source.git"
    bare.mkdir(parents=True)
    bad = bare / relative
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("untrusted")
    with pytest.raises(SemanticError, match="SUPPLEMENT_UNSAFE_GIT_LAYOUT"):
        source.validate_layout(bare, lambda: None)


def test_unknown_identity_refused_before_creating_output(tmp_path):
    output = tmp_path / "not-created"
    with pytest.raises(SemanticError, match="SUPPLEMENT_SOURCE_NOT_APPROVED"):
        source.acquire("arbitrary/repository", "0" * 40, output)
    assert not output.exists()


def test_output_outside_runtime_refused_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr(assets, "ROOT", tmp_path)
    with pytest.raises(assets.AssetError):
        source.acquire("apache/commons-collections", HEAD, tmp_path / "elsewhere")
    assert not (tmp_path / "elsewhere").exists()


def test_existing_published_name_is_not_trusted_from_its_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(assets, "ROOT", tmp_path)
    root = tmp_path / "runtime" / "new-sources"
    root.mkdir(parents=True)
    target = root / ("commons-collections-" + HEAD)
    target.mkdir()
    (target / "acquisition.json").write_text('{"self_sealed":true}')
    with pytest.raises(SemanticError, match="SUPPLEMENT_SOURCE_EXISTS"):
        source.acquire("apache/commons-collections", HEAD, root)
    assert list(root.iterdir()) == [target]


def test_layout_check_propagates_cancellation_without_suppressing_it(tmp_path, monkeypatch):
    monkeypatch.setattr(assets, "ROOT", tmp_path)
    bare = tmp_path / "runtime" / "source.git"
    bare.mkdir(parents=True)

    def cancel():
        raise SemanticError("SUPPLEMENT_TEST_CANCEL")

    with pytest.raises(SemanticError, match="SUPPLEMENT_TEST_CANCEL"):
        source.validate_layout(bare, cancel)


def test_isolated_environment_reaches_owned_init_without_credentials(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(assets, "ROOT", tmp_path)
    monkeypatch.setattr(source.artifacts, "sources", lambda _: {"fixture": "fixed"})
    monkeypatch.setattr(source.URLPolicy, "addresses", lambda self, target: ("8.8.8.8",))
    monkeypatch.setenv("GIT_SSH_COMMAND", "untrusted-fixture-command")
    monkeypatch.setenv("GITHUB_TOKEN", "inert-test-fixture")

    class NoNetworkTunnel:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return SimpleNamespace(port=12345, bytes=0)

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(source, "Tunnel", NoNetworkTunnel)

    def owned_init(arguments, **kwargs):
        assert arguments[1] == "init"
        env = kwargs["environment"]
        assert "GIT_SSH_COMMAND" not in env and "GITHUB_TOKEN" not in env
        config = {
            env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
            for i in range(int(env["GIT_CONFIG_COUNT"]))
        }
        assert config["protocol.allow"] == "never"
        assert config["protocol.https.allow"] == "always"
        assert config["http.sslVerify"] == "true" and config["http.followRedirects"] == "false"
        assert config["credential.helper"] == "" and Path(config["core.hooksPath"]).is_dir()
        raise SemanticError("SUPPLEMENT_TEST_INIT_REACHED")

    monkeypatch.setattr(source.native_runtime, "run", owned_init)
    with pytest.raises(SemanticError, match="SUPPLEMENT_TEST_INIT_REACHED"):
        source.acquire("apache/commons-collections", HEAD, tmp_path / "runtime" / "sources")
