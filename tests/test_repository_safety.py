import http.client
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.repositories.clone import CloneExecutor
from packages.repositories.safety import RepositoryError, URLPolicy, canonicalize, public_address
from packages.repositories.storage import GIB, Storage, directory_bytes
from packages.repositories.transport import RepositoryProbe, Tunnel
from tests.repository_support import git, https_git


@pytest.mark.parametrize(
    "value",
    [
        "http://github.com/a/b",
        "ssh://github.com/a/b",
        "file:///a/b",
        "https://u:p@github.com/a/b",
        "https://@github.com/a/b",
        "https://github.com:444/a/b",
        "https://github.com/a/b?",
        "https://github.com/a/b#",
        "https://github.com/a/%2e",
        "https://github.com/a/../b",
        "https://github.com/a/b\n",
        "https://localhost/a/b",
        "https://host.local/a/b",
        "https://github.com/a\\b",
        "https://[::1]/a/b",
        "https://github.com/a/b/extra",
    ],
)
def test_unsafe_url(value):
    with pytest.raises(RepositoryError) as error:
        canonicalize(value)
    assert error.value.code == "REPOSITORY_UNSAFE_URL"


def test_canonical_identity():
    assert canonicalize("https://GitHub.Com:443/TEAM/Demo.git/").url == (
        "https://github.com/team/demo.git"
    )
    assert canonicalize("https://repo.example/TEAM/Demo").path == "/TEAM/Demo.git"


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "192.0.0.9",
        "192.0.2.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:8.8.8.8",
        "64:ff9b::808:808",
        "2002:808:808::1",
        "2001:db8::1",
        "ff02::1",
        "garbage",
    ],
)
def test_nonpublic_addresses(address):
    with pytest.raises(RepositoryError):
        public_address(address)


def test_all_dns_answers_and_numeric_pin():
    target = canonicalize("https://repo.example/team/demo")
    assert URLPolicy(lambda _: ["8.8.8.8", "2606:4700:4700::1111"]).addresses(target) == (
        "8.8.8.8",
        "2606:4700:4700::1111",
    )
    with pytest.raises(RepositoryError):
        URLPolicy(lambda _: ["8.8.8.8", "::1"]).addresses(target)
    answers = iter([["8.8.8.8"], ["127.0.0.1"]])
    policy = URLPolicy(lambda _: next(answers))
    assert policy.addresses(target) == ("8.8.8.8",)
    connected = []
    with Tunnel(target, policy, 1000, lambda address: connected.append(address)) as tunnel:
        client = http.client.HTTPConnection("127.0.0.1", tunnel.port)
        client.request("CONNECT", "repo.example:443")
        assert client.getresponse().status == 403
        client.close()
        assert tunnel.error == "REPOSITORY_UNSAFE_ADDRESS"
    assert connected == []


def test_tunnel_rejects_authority_plain_http_and_peer_mismatch():
    target = canonicalize("https://repo.example/team/demo")
    calls = []
    peer = SimpleNamespace(getpeername=lambda: ("127.0.0.1", 443), close=lambda: None)
    with Tunnel(
        target,
        URLPolicy(lambda _: ["8.8.8.8"]),
        1000,
        lambda address: calls.append(address) or peer,
    ) as tunnel:
        for method, path in (("CONNECT", "other.example:443"), ("GET", "http://repo.example/")):
            client = http.client.HTTPConnection("127.0.0.1", tunnel.port)
            client.request(method, path)
            assert client.getresponse().status == 403
            client.close()
        assert calls == []
        with pytest.raises(RepositoryError):
            tunnel.upstream()
        with pytest.raises(RepositoryError):
            tunnel.account(1001)


def test_real_git_advertisement_redirect_and_verified_tls(tmp_path):
    with https_git(tmp_path / "tls") as fixture:
        target = canonicalize("https://repo.example/team/demo")
        fixture["probe"].check(target)
        with pytest.raises(RepositoryError) as failure:
            RepositoryProbe(fixture["policy"], fixture["connector"]).check(target)
        assert failure.value.code == "REPOSITORY_NETWORK_UNAVAILABLE"
        fixture["state"]["redirect"] = True
        with pytest.raises(RepositoryError) as failure:
            fixture["probe"].check(target)
        assert failure.value.code == "REPOSITORY_REDIRECT_REJECTED"
        assert not any("private" in path for path in fixture["state"]["requests"])


def test_growth_reserve_and_owned_cleanup(tmp_path):
    settings = SimpleNamespace(repository_storage_root=tmp_path, repository_max_bytes=GIB)
    storage = Storage(settings, lambda _: SimpleNamespace(free=3 * GIB))
    storage.capacity()
    with pytest.raises(RepositoryError) as failure:
        storage.preflight()
    assert failure.value.code == "REPOSITORY_STORAGE_LOW"
    attempt = storage.attempt(str(uuid4()), str(uuid4()))
    (attempt / "owned.txt").write_text("own")
    with pytest.raises(RepositoryError):
        storage.remove(tmp_path)
    storage.remove(attempt)
    assert not attempt.exists() and tmp_path.exists()


def test_size_scan_tolerates_index_pack_rename(tmp_path, monkeypatch):
    temporary, kept = tmp_path / "tmp_idx", tmp_path / "pack.idx"
    temporary.write_bytes(b"temporary")
    kept.write_bytes(b"kept")
    real_lstat = Path.lstat

    def disappear(path, *args, **kwargs):
        if path == temporary and path.exists():
            path.unlink()
            raise FileNotFoundError
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", disappear)
    assert directory_bytes(tmp_path) == 4


@pytest.mark.parametrize(
    "program,deadline,code",
    [
        ("import time; time.sleep(30)", -1, "REPOSITORY_CLONE_TIMEOUT"),
        ("import sys; sys.stdout.write('x'*100000)", 20, "REPOSITORY_OUTPUT_LIMIT"),
    ],
)
def test_supervised_process_limits_and_cleanup(tmp_path, program, deadline, code):
    settings = SimpleNamespace(repository_storage_root=tmp_path)
    runner = CloneExecutor(settings, None, git=sys.executable)
    with pytest.raises(RepositoryError) as failure:
        runner.command(["-c", program], {}, tmp_path, lambda: None, time.monotonic() + deadline)
    assert failure.value.code == code
    assert not (tmp_path / "command.out").exists()
    assert not (tmp_path / "command.err").exists()


@pytest.mark.parametrize("empty", [False, True])
def test_cross_platform_native_git_clone_with_verified_tls(tmp_path, empty):
    root = tmp_path / "repos"
    root.mkdir()
    settings = SimpleNamespace(
        repository_storage_root=root,
        repository_max_bytes=64 * 1024**2,
        repository_timeout_seconds=30,
        task_heartbeat_seconds=1,
    )
    published, failed = [], []
    service = SimpleNamespace(
        tasks=SimpleNamespace(checkpoint=lambda *args, **kwargs: True),
        publish=lambda *args: published.append(args) or True,
        fail=lambda *args: failed.append(args),
    )
    with https_git(tmp_path / "tls", empty=empty) as fixture:
        runner = CloneExecutor(
            settings, service, fixture["policy"], fixture["connector"], cafile=fixture["certfile"]
        )
        assert runner.run(
            str(uuid4()),
            str(uuid4()),
            {"repository_id": str(uuid4()), "url": "https://repo.example/team/demo"},
        )
        assert not failed
        metadata, path = published[0][2:]
        assert metadata["head_sha"] == fixture["head"]
        assert metadata["default_branch"] == (None if empty else "main")
        assert metadata["size_bytes"] > 0
        assert git("-C", str(root / path), "rev-parse", "--is-bare-repository").strip() == b"true"
